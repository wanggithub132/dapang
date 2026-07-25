import logging
import re


def log(msg):
    """同时 print + logging.info，方便本地调试和远端日志"""
    print(msg)
    logging.info(msg)


def log_warn(msg):
    """同时 print + logging.warning"""
    print(f"[WARNING] {msg}")
    logging.warning(msg)


def log_error(msg):
    """同时 print + logging.error"""
    print(f"[ERROR] {msg}")
    logging.error(msg)


def log_debug(msg):
    """同时 print + logging.debug"""
    print(f"[DEBUG] {msg}")
    logging.debug(msg)


# 去除所有表情
def clean(desstr, restr=''):
    # 过滤表情
    try:
        co = re.compile(u'['u'\U0001F300-\U0001F64F' u'\U0001F680-\U0001F6FF'u'\u2600-\u2B55]+')
    except re.error:
        co = re.compile(u'('u'\ud83c[\udf00-\udfff]|'u'\ud83d[\udc00-\ude4f\ude80-\udeff]|'u'[\u2600-\u2B55])+')
    return co.sub(restr, desstr)