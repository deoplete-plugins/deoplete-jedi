import traceback
import re
import jedi

from .base import Base


class Source(Base):

    def __init__(self, vim):
        Base.__init__(self, vim)

        self.name = 'jedi'
        self.mark = '[jedi]'
        self.filetypes = ['python']
        self.rank = 1000
        self.min_pattern_length = 0
        self.input_pattern = r'[^. \t0-9]\.\w*|^\s*@\w*|^\s*from\s.+import \w*|^\s*from \w*|^\s*import \w*'
        self.is_bytepos = True

    def get_complete_position(self, context):
        return self.completions(1, 0)

    def gather_candidates(self, context):
        return self.completions(0, 0)

    def get_script(self, source=None, column=None):
        # http://jedi.jedidjah.ch/en/latest/docs/settings.html
        jedi.settings.additional_dynamic_modules = \
            [b.name for b in self.vim.buffers if b.name is not None and b.name.endswith('.py')]
        jedi.settings.add_dot_after_module = True
        jedi.settings.add_bracket_after_function = True

        if source is None:
            source = '\n'.join(self.vim.current.buffer)
        row = self.vim.current.window.cursor[0]
        if column is None:
            column = self.vim.current.window.cursor[1]
        buf_path = self.vim.current.buffer.name
        encoding = self.vim.eval('&encoding')

        return jedi.Script(source, row, column, buf_path, encoding)

    def completions(self, findstart, base):
        row, column = self.vim.current.window.cursor

        if findstart == 1:
            return column
        else:
            source = ''
            for i, line in enumerate(self.vim.current.buffer):
                if i == row - 1:
                    source += line[:column] + str(base) + line[column:]
                else:
                    source += line
                source += '\n'

            script = self.get_script(source=source, column=column)
            completions = script.completions()

            out = []
            for c in completions:
                d = dict(word=c.complete,
                         abbr=c.name,
                         kind=c.description,
                         info=c.docstring(),
                         icase=1,
                         dup=1
                         )
                out.append(d)

            return out
