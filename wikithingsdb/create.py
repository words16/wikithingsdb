#!/usr/bin/env python2

import argparse
from wikimap import data
from defexpand import infoclass
from wikithingdb.engine import engine
from wikithingdb import models
from sqlalchemy.orm import sessionmaker

Session = sessionmaker(bind=engine)


# DB-INTERFACING
def insert_article_wiki_classes(article, classes):
    """Given article (str) and classes (list of str), inserts into
    DB
    """
    # TODO: how to deal with already existing page table?
    # need to backreference page_id or something?

def insert_class_dbpedia_classes(w_class, dbp_classes):
    """Given class (str) and list of dbpedia_classes (list of str),
    insterts into DB
    """    
    wc = WikiClass(w_class)
    session.add(wc)
    for dbp_class in dbp_classes:
        wc.dbpedia_classes.append(DbpediaClass(dbp_class))


def insert_article_types(article, types):
    """Given article (str) and list of types (list of str), inserts
    into DB
    """
    # TODO: how to deal with already existing page table?
    # need to backreference page_id or something?


# DATA RETRIEVAL
def get_all_article_classes():
    # for each article, get inofoboxes
    # insert_article_wiki_classes(article, infoboxes)


def get_all_class_hypernyms(infoboxes):
    for infobox in infoboxes:
        hypernyms = infoclass.classes_above_infobox(infobox)
        insert_class_dbpedia_classes(infobox, hypernyms)


def get_all_article_types():
    # for each article, get types using Whoami (need a way to do this)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("excel",
                        help="path to input excel of infobox templates")
    args = parser.parse_args()

    get_all_class_hypernyms(data.get_infoboxes(args.excel))

    session.commit()


if __name__ == '__main__':
    main()