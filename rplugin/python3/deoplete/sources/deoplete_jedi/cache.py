import os
import re
import glob
import json
import time
import hashlib
import logging
import threading
import subprocess

_paths = []
_cache_path = None
# List of items in the file system cache. `import~` is a special key for
# caching import modules. It should not be cached to disk.
_file_cache = set(['import~'])

_cache_lock = threading.RLock()
_cache = {}

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
        self.completions = dict.get('completions')
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
        with _cache_lock:
            self._touched = time.time()

    def to_dict(self):
        return {
            'cache_key': self.key,
            'time': self.time,
            'modules': self.modules,
            'completions': self.completions,
        }


def get_cache_path():
    global _cache_path
    if not _cache_path or not os.path.isdir(_cache_path):
        p = subprocess.Popen(['python', '-V'], stdout=subprocess.PIPE,
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
        if len(key) == 1 and key[0] not in _file_cache:
            # This will only load the cached item from a file the first time it
            # was seen.
            cache_file = os.path.join(get_cache_path(), '{}.json'.format(key[0]))
            if os.path.isfile(cache_file):
                _file_cache.add(key[0])
                with open(cache_file, 'rt') as fp:
                    try:
                        cached = CacheEntry(json.load(fp))
                        cached.time = time.time()
                        _cache[key] = cached
                        log.debug('Loaded from file: %r', key)
                        return cached
                    except Exception:
                        pass
        return _cache.get(key)


def store(key, value):
    with _cache_lock:
        if not isinstance(value, CacheEntry):
            value = CacheEntry(value)

        if value.refresh:
            # refresh is set when completions is None.  This will be due to
            # Jedi producing an error and not getting any completions.  Use any
            # previously cached completions while a refresh is attempted.
            old = _cache.get(key)
            value.completions = old.completions

        _cache[key] = value

        if len(key) == 1 and key[0] not in _file_cache:
            _file_cache.add(key[0])
            cache_file = os.path.join(get_cache_path(), '{}.json'.format(key[0]))
            with open(cache_file, 'wt') as fp:
                json.dump(value.to_dict(), fp)
                log.debug('Stored to file: %r', key)
        return value


def exists(key):
    return key in _cache


def reaper(max_age=300):
    """Clear the cache of old items

    Module level completions are exempt from reaping.  It is assumed that
    module level completions will have a key length of 1.
    """
    last = time.time()
    while True:
        n = time.time()
        if n - last < 30:
            time.sleep(0.05)
            continue
        last = n

        with _cache_lock:
            cl = len(_cache)
            for cached in list(_cache.values()):
                if len(cached.key) > 1 and n - cached._touched > max_age:
                    _cache.pop(cached.key)
            reaped = cl - len(_cache)
            if reaped > 0:
                log.debug('Removed %d of %d cache items', reaped, cl)


def start_reaper():
    log.debug('Starting reaper thread')
    t = threading.Thread(target=reaper)
    t.daemon = True
    t.start()


# balanced() taken from:
# http://stackoverflow.com/a/6753172/4932879
# Modified to include string delimiters
def _balanced():
    # Doc strings might be an issue, but we don't care.
    idelim = iter("""(){}[]""''""")
    delims = dict(zip(idelim, idelim))
    closing = delims.values()

    def balanced(astr):
        stack = []
        skip = False
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
                stack.append(d)
            elif c in closing:
                if c == open_str:
                    open_str = ''
                if not open_str and (not stack or c != stack.pop()):
                    return False
        return not stack
    return balanced
balanced = _balanced()


def split_module(text, default_value=None):
    """Utility to split the module text.

    If there is nothing to split, return `default_value`.
    """
    m = re.search('([\S\.]+)$', text)
    if m and '.' in m.group(1):
        if not balanced(text):
            # Handles cases where the cursor is inside of unclosed delimiters.
            return default_value
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


def is_package(module, refresh=False):
    """Test if a module path is an installed package

    The current interpreter's sys.path is retrieved on first run.
    """
    if re.search(r'[^\w\.]', module):
        return False

    global _paths
    if not _paths or refresh:
        p = subprocess.Popen([
            'python',
            '-c', r'import sys; print("\n".join(sys.path))',
        ], stdout=subprocess.PIPE)
        stdout, _ = p.communicate()
        _paths = [x for x in stdout.decode('utf8').split('\n')
                  if x and os.path.isdir(x)]

    module = module.split('.', 1)[0]
    pglobs = [os.path.join(x, module, '__init__.py') for x in _paths]
    pglobs.extend([os.path.join(x, '{}.*'.format(module)) for x in _paths])
    return any(map(glob.glob, pglobs))


def cache_context(filename, context, source):
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
    deoplete_input = context['input'].lstrip()
    if not re.sub(r'[\s\d\.]+', '', deoplete_input):
        return None, []
    filename_hash = hashlib.md5(filename.encode('utf8')).hexdigest()
    line = context['position'][1]
    log.debug('Input: "%s"', deoplete_input)
    cache_key = None
    extra_modules = []
    cur_module = os.path.splitext(os.path.basename(filename))[0]

    if deoplete_input.startswith(('import ', 'from ')):
        # Cache imports with buffer filename as the key prefix.
        # For `from` imports, the first part of the statement is
        # considered to be the same as `import` for caching.

        import_key = 'import~'
        deoplete_input = context['input'].lstrip()
        m = re.search(r'^from\s+(\S+)', deoplete_input)
        if m:
            import_key = m.group(1)

        if import_key:
            cache_key = (import_key,)

    if not cache_key:
        obj = split_module(deoplete_input.strip())
        if obj:
            cache_key = (obj,)
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
                    cache_key = (module_path,)
                else:
                    # A quick scan revealed that the dot completion doesn't
                    # involve an imported module.  Treat it like a scoped
                    # variable and ensure the cache invalidates when the file
                    # is saved.
                    parents = get_parents(source, line)
                    parents.insert(0, cur_module)
                    cache_key = (filename_hash, tuple(parents), obj, 'dot')
                    if os.path.exists(filename):
                        extra_modules.append(filename)
        elif context.get('complete_str'):
            parents = get_parents(source, line)
            parents.insert(0, cur_module)
            cache_key = (filename_hash, tuple(parents), 'vars')
            if os.path.exists(filename):
                extra_modules.append(filename)

    return cache_key, extra_modules
