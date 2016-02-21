from os.path import abspath
from os.path import dirname
from os.path import join
import sys

def set_debug(logger, path):
    from logging import FileHandler, Formatter, DEBUG
    hdlr = FileHandler(path)
    logger.addHandler(hdlr)
    datefmt = '%Y/%m/%d %H:%M:%S'
    fmt = Formatter(
        "%(levelname)s %(asctime)s %(message)s", datefmt=datefmt)
    hdlr.setFormatter(fmt)
    logger.setLevel(DEBUG)

def load_external_module(module):
    current = dirname(abspath(__file__))
    module_dir = join(dirname(current), module)
    sys.path.insert(0, module_dir)

def get_var(vim, variable):
    try:
        return vim.vars[variable]
    except Exception:
        return None

