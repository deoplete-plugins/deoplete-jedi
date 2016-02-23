import os
import re

from deoplete.sources.base import Base
from deoplete.util import load_external_module
from logging import getLogger

current = __file__
load_external_module(current, 'jedi')
import jedi

load_external_module(current, 'sources/deoplete_jedi')
from profiler import timeit

logger = getLogger(__name__)


class Source(Base):

    def __init__(self, vim):
        Base.__init__(self, vim)

        self.name = 'jedi'
        self.mark = '[jedi]'
        self.rank = 500
        self.filetypes = ['python']
        self.input_pattern = (r'[^. \t0-9]\.\w*$|'
                              r'^\s*@\w*$|'
                              r'^\s*from\s.+import \w*|'
                              r'^\s*from \w*|'
                              r'^\s*import \w*')

        try:
            if self.vim.vars['deoplete#enable_debug']:
                from helper import set_debug
                log_file = \
                    self.vim.vars['deoplete#sources#jedi#debug#log_file']
                set_debug(logger, os.path.expanduser(log_file))
        except Exception:
            pass

    def get_complete_position(self, context):
        m = re.search(r'\w*$', context['input'])
        return m.start() if m else -1

    # @timeit(logger, 'simple', [0.10000000, 0.20000000])
    def gather_candidates(self, context):
        line = self.vim.eval("line('.')")
        col = context['complete_position']
        buf = self.vim.current.buffer
        source = '\n'.join(buf[:])
        cline = self.vim.current.line

        try:
            completions = \
                self.get_script(
                    source, line, col, buf.name).completions()
        except Exception:
            return []

        out = []
        for c in completions:
            word = c.name

            # TODO(zchee): configurable and refactoring
            # Add '(' bracket
            if not self.is_import(cline) and c.type == 'function':
                word += '('
            # Add '.' for 'self' and 'class'
            elif not self.is_import(cline) and \
                not re.search(r'Error|Exception', word) and \
                (word == 'self' or
                 c.type == 'module' or
                 c.type == 'class'):
                word += '.'

            # Format c.docstring() for abbr
            if re.match(c.name, c.docstring()):
                abbr = re.sub('"(|)|  ",', '',
                              c.docstring().split("\n\n")[0]
                              .split("->")[0]
                              .replace('\n', ' ')
                              )
            else:
                abbr = c.name

            out.append(dict(word=word,
                            abbr=abbr,
                            kind=re.sub('\n|  ', '', c.description),
                            info=c.docstring(),
                            dup=1
                            ))

        return out

    def is_import(self, line):
        return re.match(r'^\s*from\s.+import \w*|'
                        r'^\s*from \w*|'
                        r'^\s*import \w*',
                        line)

    def get_script(self, source, line, col, buf_path):
        # http://jedi.jedidjah.ch/en/latest/docs/settings.html#jedi.settings.add_dot_after_module
        # Adds a dot after a module, because a module that is not accessed this
        # way is definitely not the normal case.  However, in VIM this doesn’t
        # work, that’s why it isn’t used at the moment.
        jedi.settings.add_dot_after_module = True

        # http://jedi.jedidjah.ch/en/latest/docs/settings.html#jedi.settings.add_bracket_after_function
        # Adds an opening bracket after a function, because that's normal
        # behaviour.  Removed it again, because in VIM that is not very
        # practical.
        jedi.settings.add_bracket_after_function = True

        # http://jedi.jedidjah.ch/en/latest/docs/settings.html#jedi.settings.additional_dynamic_modules
        # Additional modules in which Jedi checks if statements are to be
        # found.  This is practical for IDEs, that want to administrate their
        # modules themselves.
        jedi.settings.additional_dynamic_modules = [
            b.name for b in self.vim.buffers
            if b.name is not None and b.name.endswith('.py')]

        cache_home = os.getenv('XDG_CACHE_HOME')
        if cache_home is None:
            cache_home = os.path.expanduser('~/.cache')
        jedi.settings.cache_directory = os.path.join(cache_home, 'jedi')

        return jedi.Script(source, line, col, buf_path)
