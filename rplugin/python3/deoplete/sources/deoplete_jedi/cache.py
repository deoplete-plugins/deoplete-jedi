import os
import re


def split_module(text, default_value=None):
    """Utility to split the module text.

    If there is nothing to split, return `default_value`.
    """
    m = re.search('([\w_\.]+)$', text)
    if m and '.' in m.group(1):
        return m.group(1).rsplit('.', 1)[0]
    return default_value


def cache_context(filename, context):
    """Caching based on context input.

    If the input is blank, it was triggered with `.` to get module completions.

    The module files as reported by Jedi are stored with their modification
    times to help detect if a cache needs to be refreshed.

    For scoped variables in the buffer, construct a cache key using the
    filename.  The buffer file's modification time is checked to see if the
    completion needs to be refreshed.  The approximate scope lines are cached
    to help invalidate the cache based on line position.
    """
    line = context['position'][1]
    deoplete_input = context['input'].strip()
    cache_key = None
    extra_modules = []
    cache_line = 0

    if deoplete_input.startswith(('import ', 'from ')):
        # Cache imports with buffer filename as the key prefix.
        # For `from` imports, the first part of the statement is
        # considered to be the same as `import` for caching.
        suffix = 'import'

        # The trailing whitespace is significant for caching imports.
        import_line = context['input'].lstrip()

        if import_line.startswith('import'):
            m = re.search(r'^import\s+(\S+)$', import_line)
        else:
            m = re.search(r'^from\s+(\S+)\s+import\s+', import_line)
            if m:
                # Treat the first part of the import as a cached
                # module, but cache it per-buffer.
                cache_key = '{}.from.{}'.format(filename, m.group(1))
            else:
                m = re.search(r'^from\s+(\S+)$', import_line)

        if not cache_key:
            if m:
                suffix = split_module(m.group(1), suffix)
                cache_key = '{}.import.{}'.format(filename, suffix)
                if os.path.exists(filename):
                    extra_modules.append(filename)

    if not cache_key:
        # Find a cacheable key first
        cache_key = split_module(deoplete_input)
        if cache_key:
            if cache_key.startswith('self'):
                # TODO: Get class lines and cache these differently
                # based on cursor position.
                # Cache `self.`, but monitor buffer file's modification
                # time.
                if os.path.exists(filename):
                    extra_modules.append(filename)
                cache_key = '{}.{}'.format(filename, cache_key)
                cache_line = line - 1
                os.path
        elif context.get('complete_str'):
            # Note: Module completions will be an empty string.
            cache_key = filename
            if os.path.exists(filename):
                extra_modules.append(filename)
            cache_line = line - 1

    return cache_key, cache_line, extra_modules
