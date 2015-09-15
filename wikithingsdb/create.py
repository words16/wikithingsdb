#!/usr/bin/env python2

import argparse
import codecs
import re
import requests
from requests.auth import HTTPBasicAuth
import logging
import time
from Queue import Queue
from threading import Thread
from unidecode import unidecode
from defexpand import infoclass
from nltk.tokenize import sent_tokenize
from wikithingsdb.engine import engine
from wikithingsdb.models import Page, WikiClass, Type, DbpediaClass
from wikithingsdb import config
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.expression import ClauseElement

Session = sessionmaker(bind=engine)
session = Session()
logger = logging.getLogger(__name__)

ontology = infoclass.get_info_ontology()

# XML article header
HEADER_STR = "<doc id="
HEADER_PATTERN = re.compile(
    r'<doc id=\"(\d+)\" url=\"(.+?)\" title=\"(.+?)\" infobox=\"(.*)\">')

# queues for multithreading
input = Queue()
output = Queue()
POISON = '<poison></poison>'  # signal for a thread to die

# DATA RETRIEVAL


def get_header_fields(header):
    m = re.match(HEADER_PATTERN, header.strip())
    if m:
        id = m.group(1)
        title = m.group(3)
        if m.group(4).strip():
            infoboxes = m.group(4).split(', ')
        else:
            infoboxes = []
        return id, title, infoboxes
    else:
        raise ValueError("Invalid header format: %s" % header)


def get_first_sentence(article_text):
    # first line is the title, skip
    first_paragraph = article_text.split('\n')[3]
    if first_paragraph:
        sentence = sent_tokenize(first_paragraph)[0]
        sentence = unidecode(sentence)
    else:
        sentence = ''

    return sentence


def get_types(sentence):
    """for each article, get types of an article using Whoami using
    first sentence
    """
    url = 'http://%s:%s/define' % (config.WHOAMI_HOST, config.WHOAMI_PORT)
    params = {'sentence': sentence}

    try:
        if config.WHOAMI_USE_AUTH:
            auth = HTTPBasicAuth(config.WHOAMI_USERNAME, config.WHOAMI_PASSWORD)
            r = requests.get(url, params=params, auth=auth)
        else:
            r = requests.get(url, params=params)

        if r.status_code != 200:
            return []
    except Exception as e:
        logger.exception(e)

    _result = r.json()
    definitions = _result['definitions']

    return [d.encode('utf-8') for d in definitions]


def ignore_types(title, sentence):
    if sentence == '':
        return True

    # Using string contains because it's faster than regex search
    disambig_pattern = '(disambiguation)'
    listof_pattern = 'List of'
    referto_pattern = 'may refer to'

    is_disambig = disambig_pattern in title
    is_list = listof_pattern in title
    is_refer = referto_pattern in sentence

    return is_disambig or is_list or is_refer


def get_class_hypernyms(infoboxes):
    return {infobox: ontology.classes_above_infobox(infobox)
            for infobox in infoboxes}
    # for infobox in infoboxes:
    #     hypernyms = ontology.classes_above_infobox(infobox)
    #     insert_class_dbpedia_classes(infobox, hypernyms)


def get_fields(article):
    lines = article.split('\n')
    header = lines[0]
    id, title, infoboxes = get_header_fields(header)
    id = int(id)

    text = '\n'.join(lines[1:-1])
    sentence = get_first_sentence(text)

    types = []
    if not ignore_types(title, sentence):
        types = get_types(sentence)

    hypernyms = get_class_hypernyms(infoboxes)

    return id, infoboxes, types, hypernyms

# DB-INSERTION


def insert_article_classes_types(article_id, w_classes, a_types):
    """Given an article's id (int) and classes (list of str), inserts
    into DB
    """
    a = session.query(Page).get(article_id)
    for w_class in w_classes:
        a.classes.append(
            _get_or_create(session, WikiClass, class_name=w_class))
    for a_type in a_types:
        a.types.append(_get_or_create(session, Type, type=a_type))

    # print "ARTICLE-CLASSES-TYPES:"
    # print "article id: " + article_id
    # print "infoboxes:"
    # print w_classes
    # print "types:"
    # print a_types
    # print "----------------------------------"


def insert_class_dbpedia_classes(hypernym_dict):
    """Given class (str) and list of dbpedia_classes (list of str),
    insterts into DB
    """
    for w_class, dbp_classes in hypernym_dict.iteritems():
        wc = _get_or_create(session, WikiClass, class_name=w_class)
        session.add(wc)

        for dbp_class in dbp_classes:
            wc.dbpedia_classes.append(
                _get_or_create(session, DbpediaClass, dpedia_class=dbp_class))

        # print "DBPEDIA-CLASSES:"
        # print "infobox: " + w_class
        # print "hypernyms:"
        # print dbp_classes
        # print "----------------------------------"


def _get_or_create(session, model, defaults={}, **kwargs):
    try:
        query = session.query(model).filter_by(**kwargs)

        instance = query.first()

        if instance:
            return instance
        else:
            session.begin(nested=True)
            try:
                params = {k: v for k, v in kwargs.iteritems() if not isinstance(v, ClauseElement)}
                params.update(defaults)
                instance = model(**params)

                session.add(instance)
                session.commit()

                return instance
            except IntegrityError as e:
                session.rollback()
                instance = query.one()

                return instance
    except Exception as e:
        raise e


def db_worker(num_article_workers, commit_frequency=1000):
    num_poisons = 0
    insert_count = 0

    while num_poisons < num_article_workers:
        fields = output.get()
        if fields == POISON:
            num_poisons += 1
        else:
            logger.debug("Get fields from output queue")
            id, infoboxes, types, hypernyms = fields
            insert_article_classes_types(id, infoboxes, types)
            insert_class_dbpedia_classes(hypernyms)
            logger.debug('inserted article with id %s' % id)
            insert_count += 1

            if insert_count % commit_frequency == 0:
                logger.info('COMMIT point. Progress: %s articles'
                            % insert_count)
                session.commit()

    logger.info("DB worker received all POISONs, terminating.")


def article_worker():
    while True:
        article = input.get()
        if article == POISON:
            output.put(POISON)
            break
        else:
            logger.debug("Get article from input queue")
            try:
                result = get_fields(article)
                output.put(result)
            except Exception as e:
                logger.exception(e)
                pass

    logger.info("Article worker received POISON, terminating.")


def process_articles(args):
    path = args.merged_xml
    count = 0
    start = time.time()

    article = ''
    file = codecs.open(path, 'r', 'utf-8')

    insert_thread = Thread(target=db_worker,
                           name='DbWorker',
                           args=[args.threads],
                           kwargs={'commit_frequency': 1000})

    insert_thread.start()
    logger.info('Started db insert worker thread')

    pool = [Thread(target=article_worker, name='ArticleWorker-%s' % i)
            for i in xrange(args.threads)]

    for worker in pool:
        worker.start()

    logger.info('Started %s article worker threads.' % len(pool))

    for line in file:
        article += line
        if line == '</doc>\n':
            input.put(article)
            logger.debug("Put article to input queue")
            count += 1
            article = ''
    file.close()

    for worker in pool:
        input.put(POISON)

    logger.info('Waiting for %s threads to finish.' % len(pool))

    for worker in pool:
        worker.join()

    insert_thread.join()

    logger.info('All threads finished.')
    logger.info('Processed %s articles' % count)

    # print time information
    diff = time.time() - start
    logger.info('Time to process %d articles: %d seconds' % (count, diff))
    minutes, seconds = divmod(diff, 60)
    hours, minutes = divmod(minutes, 60)
    logger.info(' ... %d hours, %d minutes, and %f seconds'
                % (count, hours, minutes, seconds))
    minutes, seconds = divmod(diff / count, 60)
    logger.info('Average time/article: %d minutes and %f seconds'
                % (minutes, seconds))

    logger.info('This message confirms that WikiThingsDB create completed '
                'successfully.')


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("merged_xml",
                        help="path to merged xml file made with "
                        "merge_extracted.sh")
    parser.add_argument("-t",
                        "--threads",
                        help="the number of threads to run for parallel "
                        "execution. Usually the number of cores in the machine",
                        default=1,
                        type=int
                        )
    parser.add_argument("-v",
                        "--verbose",
                        help="increase output verbosity",
                        action="store_true")
    parser.add_argument("-q",
                        "--quiet",
                        help="decrease output verbosity",
                        action="store_true")
    parser.add_argument("-l",
                        "--logfile",
                        help="path to log file",
                        default="wikithingsdb-create.log"
                        )

    args = parser.parse_args()

    loglevel = logging.INFO
    if args.verbose:
        loglevel = logging.DEBUG
    if args.quiet:
        loglevel = logging.WARN

    # set up logging
    logging.basicConfig(filename=args.logfile,
                        format="%(levelname)s: %(message)s",
                        level=loglevel)

    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    process_articles(args)

    # try:
    #    insert_all(args.merged_xml)
    # except etree.XMLSyntaxError:
    #    pass
    # finally:
    #    print "**************FINAL COMMIT COMMIT COMMIT **************"
    #    session.commit()


if __name__ == '__main__':
    main()
