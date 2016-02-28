import os
import re

from deoplete.sources.base import Base
from deoplete.util import load_external_module

current = __file__
load_external_module(current, 'jedi')
load_external_module(current, 'sources/deoplete_jedi')
import jedi


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

        self.description_length = \
            self.vim.vars['deoplete#sources#jedi#statement_length']

        # jedi core library settings
        # http://jedi.jedidjah.ch/en/latest/docs/settings.html
        jedi_settings = jedi.settings
        # Completion output
        jedi_settings.case_insensitive_completion = False
        # Filesystem cache
        cache_home = os.getenv('XDG_CACHE_HOME')
        if cache_home is None:
            cache_home = os.path.expanduser('~/.cache')
        jedi_settings.cache_directory = os.path.join(cache_home, 'jedi')
        # Dynamic stuff
        jedi_settings.additional_dynamic_modules = [
            b.name for b in self.vim.buffers
            if b.name is not None and b.name.endswith('.py')]

    def get_complete_position(self, context):
        m = re.search(r'\w*$', context['input'])
        return m.start() if m else -1

    def gather_candidates(self, context):
        line = context['position'][1]
        col = context['complete_position']
        buf = self.vim.current.buffer
        source = '\n'.join(buf[:])
        cline = self.vim.current.line

        try:
            completions = \
                jedi.Script(source, line, col, buf.name).completions()
        except Exception:
            return []

        out = []
        for c in completions:
            _type = c.type
            word = c.name

            # TODO(zchee): configurable and refactoring
            # Format c.docstring() for abbr
            if re.match(c.name, c.docstring()):
                abbr = re.sub('"(|)|  ",', '',
                              c.docstring().split("\n\n")[0]
                              .split("->")[0]
                              .replace('\n', ' ')
                              )
            else:
                abbr = word

            # Add '(' bracket
            if _type == 'function':
                word += '('
            # Add '.' for 'self' and 'class'
            elif not self.is_import(cline) and \
                    _type in ['module', 'class'] or \
                    not re.search(r'Error|Exception', word) and \
                    word == 'self':
                word += '.'

            out.append(dict(word=word,
                            abbr=abbr,
                            kind=self.format_description(c.description),
                            info=c.docstring(),
                            dup=1
                            ))

        return out

    def is_import(self, line):
        return re.match(r'^\s*from\s.+import \w*|'
                        r'^\s*from \w*|'
                        r'^\s*import \w*',
                        line)

    def format_description(self, raw_desc):
        description = re.sub('\n|  ', '', raw_desc)
        if re.search(' #', description) or \
                len(raw_desc) > self.description_length:
            description = 'statement'

        return description
