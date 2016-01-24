import traceback
import re
import deoplete.util
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
        # ?
        jedi.settings.additional_dynamic_modules = \
            [b.name for b in self.vim.buffers if b.name is not None and b.name.endswith('.py')]

        if source is None:
            source = '\n'.join(self.vim.current.buffer)
        row = self.vim.current.window.cursor[0]

        if column is None:
            column = self.vim.current.window.cursor[1]
        buf_path = self.vim.current.buffer.name
        encoding = self.vim.eval('&encoding') or 'utf-8'

        # row = 1 ?
        return jedi.Script(source, 1, column, buf_path, encoding)
        # return jedi.Script(source, row, column, buf_path, encoding)

    def completions(self, findstart, base):
        column = self.vim.current.window.cursor[1]
        if findstart == '1':
            count = 0
            for char in reversed(self.vim.current.buffer[column]):
                if not re.match('[\w\d]', char):
                    break
                count += 1
            return column - count
        else:
            # Get current line
            row = self.vim.current.window.cursor[0] - 1
            # Get current line source
            source = self.vim.current.buffer[row]
            try:
                script = self.get_script(source=source, column=column)
                completions = script.completions()
                signatures = script.call_signatures()

                out = []
                for c in completions:
                    d = dict(word=c.complete,
                             abbr=c.name,
                             menu=c.description,
                             info=c.docstring(),
                             icase=1,
                             dup=1
                             )
                    out.append(d)


            except Exception:
                print(traceback.format_exc())
                out = ''
                completions = []
                signatures = []

            return out
