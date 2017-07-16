import glob
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
from itertools import chain
from string import whitespace

from deoplete_jedi import utils

_paths = []
_cache_path = None
# List of items in the file system cache. `import~` is a special key for
# caching import modules. It should not be cached to disk.
_file_cache = set(['import~'])

# Cache version allows us to invalidate outdated cache data structures.
_cache_version = 15
_cache_lock = threading.RLock()
_cache = {}

python_path = 'python'

log = logging.getLogger('deoplete.jedi.cache')

# This uses [\ \t] to avoid spanning lines
_import_re = re.compile(r'''
    ^[\ \t]*(
        from[\ \t]+[\w\.]+[\ \t]+import\s+\([\s\w,]+\)|
        from[\ \t]+[\w\.]+[\ \t]+import[\ \t\w,]+|
        import[\ \t]+\([\s\w,]+\)|
        import[\ \t]+[\ \t\w,]+
    )
''', re.VERBOSE | re.MULTILINE)


class CacheEntry(object):
    def __init__(self, dict):
        self.key = tuple(dict.get('cache_key'))
        self._touched = time.time()
        self.time = dict.get('time')
        self.modules = dict.get('modules')
        self.completions = dict.get('completions', [])
        self.refresh = False
        if self.completions is None:
            self.refresh = True
            self.completions = []

    def update_from(self, other):
        self.key = other.key
        self.time = other.time
        self.modules = other.modules
        self.completions = other.completions

    def touch(self):
        self._touched = time.time()

    def to_dict(self):
        return {
            'version': _cache_version,
            'cache_key': self.key,
            'time': self.time,
            'modules': self.modules,
            'completions': self.completions,
        }


def get_cache_path():
    global _cache_path
    if not _cache_path or not os.path.isdir(_cache_path):
        p = subprocess.Popen([python_path, '-V'], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        stdout, stderr = p.communicate()
        version = re.search(r'(\d+\.\d+)\.', (stdout or stderr).decode('utf8')).group(1)
        cache_dir = os.getenv('XDG_CACHE_HOME', '~/.cache')
        cache_dir = os.path.join(os.path.expanduser(cache_dir), 'deoplete/jedi',
                                 version)
        if not os.path.exists(cache_dir):
            umask = os.umask(0)
            os.makedirs(cache_dir, 0o0700)
            os.umask(umask)
        _cache_path = cache_dir
    return _cache_path


def retrieve(key):
    if not key:
        return None

    with _cache_lock:
        if key[-1] == 'package' and key[0] not in _file_cache:
            # This will only load the cached item from a file the first time it
            # was seen.
            cache_file = os.path.join(get_cache_path(), '{}.json'.format(key[0]))
            if os.path.isfile(cache_file):
                with open(cache_file, 'rt') as fp:
                    try:
                        data = json.load(fp)
                        if data.get('version', 0) >= _cache_version:
                            _file_cache.add(key[0])
                            cached = CacheEntry(data)
                            cached.time = time.time()
                            _cache[key] = cached
                            log.debug('Loaded from file: %r', key)
                            return cached
                    except Exception:
                        pass
        cached = _cache.get(key)
        if cached:
            cached.touch()
        return cached


def store(key, value):
    with _cache_lock:
        if not isinstance(value, CacheEntry):
            value = CacheEntry(value)

        if value.refresh:
            # refresh is set when completions is None.  This will be due to
            # Jedi producing an error and not getting any completions.  Use any
            # previously cached completions while a refresh is attempted.
            old = _cache.get(key)
            if old is not None:
                value.completions = old.completions

        _cache[key] = value

        if key[-1] == 'package' and key[0] not in _file_cache:
            _file_cache.add(key[0])
            cache_file = os.path.join(get_cache_path(), '{}.json'.format(key[0]))
            with open(cache_file, 'wt') as fp:
                json.dump(value.to_dict(), fp)
                log.debug('Stored to file: %r', key)
        return value


def exists(key):
    with _cache_lock:
        return key in _cache


def reap_cache(max_age=300):
    """Clear the cache of old items

    Module level completions are exempt from reaping.  It is assumed that
    module level completions will have a key length of 1.
    """
    while True:
        time.sleep(300)

        with _cache_lock:
            now = time.time()
            cur_len = len(_cache)
            for cached in list(_cache.values()):
                if cached.key[-1] not in ('package', 'local', 'boilerplate~',
                                          'import~') \
                        and now - cached._touched > max_age:
                    _cache.pop(cached.key)

            if cur_len - len(_cache) > 0:
                log.debug('Removed %d of %d cache items', len(_cache), cur_len)


def cache_processor_thread(compl_queue):
    errors = 0
    while True:
        try:
            compl = compl_queue.get()
            cache_key = compl.get('cache_key')
            cached = retrieve(cache_key)
            if cached is None or cached.time <= compl.get('time'):
                cached = store(cache_key, compl)
                log.debug('Processed: %r', cache_key)
            errors = 0
        except Exception as e:
            errors += 1
            if errors > 3:
                break
            log.error('Got exception while processing: %r', e)


def start_background(compl_queue):
    log.debug('Starting reaper thread')
    t = threading.Thread(target=cache_processor_thread, args=(compl_queue,))
    t.daemon = True
    t.start()
    t = threading.Thread(target=reap_cache)
    t.daemon = True
    t.start()


# balanced() taken from:
# http://stackoverflow.com/a/6753172/4932879
# Modified to include string delimiters
def _balanced():
    # Doc strings might be an issue, but we don't care.
    idelim = iter("""(){}[]""''""")
    delims = dict(zip(idelim, idelim))
    odelims = {v: k for k, v in delims.items()}
    closing = delims.values()

    def balanced(astr):
        """Test if a string has balanced delimiters.

        Returns a boolean and a string of the opened delimiter.
        """
        stack = []
        skip = False
        open_d = ''
        open_str = ''
        for c in astr:
            if c == '\\':
                skip = True
                continue
            if skip:
                skip = False
                continue
            d = delims.get(c, None)
            if d and not open_str:
                if d in '"\'':
                    open_str = d
                open_d = odelims.get(d)
                stack.append(d)
            elif c in closing:
                if c == open_str:
                    open_str = ''
                if not open_str and (not stack or c != stack.pop()):
                    return False, open_d
                if stack:
                    open_d = odelims.get(stack[-1])
                else:
                    open_d = ''
        return not stack, open_d
    return balanced
balanced = _balanced()


def split_module(text, default_value=None):
    """Utility to split the module text.

    If there is nothing to split, return `default_value`.
    """
    b, d = balanced(text)
    if not b:
        # Handles cases where the cursor is inside of unclosed delimiters.
        # If the input is: re.search(x.spl
        # The returned value should be: x
        if d and d not in '\'"':
            di = text.rfind(d)
            if di != -1:
                text = text[di+1:]
        else:
            return default_value
    m = re.search('([\S\.]+)$', text)
    if m and '.' in m.group(1):
        return m.group(1).rsplit('.', 1)[0]
    return default_value


def get_parents(source, line, class_only=False):
    """Find the parent blocks

    Collects parent blocks that contain the current line to help form a cache
    key based on variable scope.
    """
    parents = []
    start = line - 1
    indent = len(source[start]) - len(source[start].lstrip())
    if class_only:
        pattern = r'^\s*class\s+(\w+)'
    else:
        pattern = r'^\s*(?:def|class)\s+(\w+)'

    for i in range(start, 0, -1):
        s_line = source[i].lstrip()
        l_indent = len(source[i]) - len(s_line)
        if s_line and l_indent < indent:
            m = re.search(pattern, s_line)
            indent = l_indent
            if m:
                parents.insert(0, m.group(1))

    return parents


def full_module(source, obj):
    """Construct the full module path

    This finds all imports and attempts to reconstruct the full module path.
    If matched on a standard `import` line, `obj` itself is a full module path.
    On `from` import lines, the parent module is prepended to `obj`.
    """

    module = ''
    obj_pat = r'(?:(\S+)\s+as\s+)?\b{0}\b'.format(re.escape(obj.split('.', 1)[0]))
    for match in _import_re.finditer('\n'.join(source)):
        module = ''
        imp_line = ' '.join(match.group(0).split())
        if imp_line.startswith('from '):
            _, module, imp_line = imp_line.split(' ', 2)
        m = re.search(obj_pat, imp_line)
        if m:
            # If the import is aliased, use the alias as part of the key
            alias = m.group(1)
            if alias:
                obj = obj.split('.')
                obj[0] = alias
                obj = '.'.join(obj)
            if module:
                return '.'.join((module, obj))
            return obj
    return None


def sys_path(refresh=False):
    global _paths
    if not _paths or refresh:
        p = subprocess.Popen([
            python_path,
            '-c', r'import sys; print("\n".join(sys.path))',
        ], stdout=subprocess.PIPE)
        stdout, _ = p.communicate()
        _paths = [x for x in stdout.decode('utf8').split('\n')
                  if x and os.path.isdir(x)]
    return _paths


def is_package(module, refresh=False):
    """Test if a module path is an installed package

    The current interpreter's sys.path is retrieved on first run.
    """
    if re.search(r'[^\w\.]', module):
        return False

    paths = sys_path(refresh)

    module = module.split('.', 1)[0]
    pglobs = [os.path.join(x, module, '__init__.py') for x in paths]
    pglobs.extend([os.path.join(x, '{}.*'.format(module)) for x in paths])
    return any(map(glob.glob, pglobs))


def cache_context(filename, context, source, extra_path):
    """Caching based on context input.

    If the input is blank, it was triggered with `.` to get module completions.

    The module files as reported by Jedi are stored with their modification
    times to help detect if a cache needs to be refreshed.

    For scoped variables in the buffer, construct a cache key using the
    filename.  The buffer file's modification time is checked to see if the
    completion needs to be refreshed.  The approximate scope lines are cached
    to help invalidate the cache based on line position.

    Cache keys are made using tuples to make them easier to interpret later.
    """
    cinput = context['input'].lstrip().lstrip('@')
    if not re.sub(r'[\s\d\.]+', '', cinput):
        return None, []
    filename_hash = hashlib.md5(filename.encode('utf8')).hexdigest()
    line = context['position'][1]
    log.debug('Input: "%s"', cinput)
    cache_key = None
    extra_modules = []
    cur_module = os.path.splitext(os.path.basename(filename))[0]

    if cinput.startswith(('import ', 'from ')):
        # Cache imports with buffer filename as the key prefix.
        # For `from` imports, the first part of the statement is
        # considered to be the same as `import` for caching.

        import_key = 'import~'
        cinput = context['input'].lstrip()
        m = re.search(r'^from\s+(\S+)(.*)', cinput)
        if m:
            if m.group(2).lstrip() in 'import':
                cache_key = ('importkeyword~', )
                return cache_key, extra_modules
            import_key = m.group(1) or 'import~'
        elif cinput.startswith('import ') and cinput.rstrip().endswith('.'):
            import_key = re.sub(r'[^\s\w\.]', ' ', cinput.strip()).split()[-1]

        if import_key:
            if '.' in import_key and import_key[-1] not in whitespace \
                    and not re.search(r'^from\s+\S+\s+import', cinput):
                # Dot completion on the import line
                import_key, _ = import_key.rsplit('.', 1)
            import_key = import_key.rstrip('.')
            module_file = utils.module_search(
                import_key,
                chain(extra_path,
                      [context.get('cwd'), os.path.dirname(filename)],
                      utils.rplugin_runtime_paths(context)))
            if module_file:
                cache_key = (import_key, 'local')
                extra_modules.append(module_file)
            elif is_package(import_key):
                cache_key = (import_key, 'package')
            elif not cinput.endswith('.'):
                cache_key = ('import~',)
            else:
                return None, extra_modules

    if not cache_key:
        obj = split_module(cinput.strip())
        if obj:
            cache_key = (obj, 'package')
            if obj.startswith('self'):
                if os.path.exists(filename):
                    extra_modules.append(filename)
                # `self` is a special case object that needs a scope included
                # in the cache key.
                parents = get_parents(source, line, class_only=True)
                parents.insert(0, cur_module)
                cache_key = (filename_hash, tuple(parents), obj)
            else:
                module_path = full_module(source, obj)
                if module_path and not module_path.startswith('.') \
                        and is_package(module_path):
                    cache_key = (module_path, 'package')
                else:
                    # A quick scan revealed that the dot completion doesn't
                    # involve an imported module.  Treat it like a scoped
                    # variable and ensure the cache invalidates when the file
                    # is saved.
                    if os.path.exists(filename):
                        extra_modules.append(filename)

                    module_file = utils.module_search(module_path,
                                                      [os.path.dirname(filename)])
                    if module_file:
                        cache_key = (module_path, 'local')
                    else:
                        parents = get_parents(source, line)
                        parents.insert(0, cur_module)
                        cache_key = (filename_hash, tuple(parents), obj, 'dot')
        elif context.get('complete_str') or cinput.rstrip().endswith('='):
            parents = get_parents(source, line)
            parents.insert(0, cur_module)
            cache_key = (filename_hash, tuple(parents), 'vars')
            if os.path.exists(filename):
                extra_modules.append(filename)

    return cache_key, extra_modules
