import os
import re
import sys

from deoplete.sources.base import Base

current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, current_dir)
from helper import get_var
from helper import load_external_module
from helper import set_debug

load_external_module('jedi')
import jedi

from logging import getLogger
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

        if get_var(self.vim, 'deoplete#enable_debug'):
            log_file = get_var(
                self.vim, 'deoplete#sources#jedi#debug#log_file')
            set_debug(logger, os.path.expanduser(log_file))

    def get_complete_position(self, context):
        m = re.search(r'\w*$', context['input'])
        return m.start() if m else -1

    def gather_candidates(self, context):
        buf = self.vim.current.buffer
        source = '\n'.join(buf)

        try:
            completions = self.get_script(
                source, context['complete_position'], buf).completions()
        except Exception:
            return []

        is_import = re.match(r'^\s*from\s.+import \w*|'
                             r'^\s*from \w*|'
                             r'^\s*import \w*',
                             self.vim.current.line)

        out = []
        for c in completions:
            word = c.name

            # TODO(zchee): configurable and refactoring
            # Add '(' bracket
            if not is_import and c.type == 'function':
                word += '('
            # Add '.' for 'self' and 'class'
            elif not is_import and (word == 'self' or
                                    c.type == 'class' or
                                    c.type == 'module'):
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

    def get_script(self, source, column, buf):
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

        line = self.vim.eval("line('.')")

        return jedi.Script(source, line, column, buf.name)
