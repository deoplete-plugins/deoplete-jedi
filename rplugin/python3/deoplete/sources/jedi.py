import re
import jedi

from .base import Base


class Source(Base):

    def __init__(self, vim):
        Base.__init__(self, vim)

        self.name = 'jedi'
        self.mark = '[jedi]'
        self.filetypes = ['python']
        self.min_pattern_length = 0
        self.input_pattern = r'[^. \t0-9]\.\w*|^\s*@\w*|^\s*from\s.+import \w*|^\s*from \w*|^\s*import \w*'
        self.is_bytepos = True

    def get_complete_position(self, context):
        return self.completions(1, 0)

    def gather_candidates(self, context):
        return self.completions(0, 0)

    def get_script(self, source=None, column=None):
        # http://jedi.jedidjah.ch/en/latest/docs/settings.html#jedi.settings.add_dot_after_module
        # Adds a dot after a module, because a module that is not accessed this way is definitely not the normal case.
        # However, in VIM this doesn’t work, that’s why it isn’t used at the moment.
        jedi.settings.add_dot_after_module = True

        # http://jedi.jedidjah.ch/en/latest/docs/settings.html#jedi.settings.add_bracket_after_function
        # Adds an opening bracket after a function, because that's normal behaviour.
        # Removed it again, because in VIM that is not very practical.
        jedi.settings.add_bracket_after_function = True

        # http://jedi.jedidjah.ch/en/latest/docs/settings.html#jedi.settings.additional_dynamic_modules
        # Additional modules in which Jedi checks if statements are to be found.
        # This is practical for IDEs, that want to administrate their modules themselves.
        jedi.settings.additional_dynamic_modules = \
            [b.name for b in self.vim.buffers if b.name is not None and b.name.endswith('.py')]

        # Need?
        if source is None:
            source = '\n'.join(self.vim.current.buffer)
        row = self.vim.current.window.cursor[0]
        # Need?
        if column is None:
            column = self.vim.current.window.cursor[1]
        buf_path = self.vim.current.buffer.name
        encoding = self.vim.eval('&encoding')

        return jedi.Script(source, row, column, buf_path, encoding)

    def completions(self, findstart, base):
        row, column = self.vim.current.window.cursor

        if findstart == 1:
            # Really good?
            return column
        else:
            # jedi-vim style? or simple?
            # source = '\n'.join(self.vim.current.buffer[:])
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
                if c.type == 'function' and re.match(c.name, c.docstring()):
                    word = c.complete + '('
                    abbr = re.sub('"(|)",|  ', '',
                                  c.docstring().split("\n\n")[0].replace('\n', ' '))
                else:
                    word = c.complete
                    abbr = c.name
                d = dict(word=str(word),
                         abbr=str(abbr),
                         kind=c.description,
                         info=c.docstring(),
                         icase=1,
                         dup=1
                         )
                out.append(d)

            return out
