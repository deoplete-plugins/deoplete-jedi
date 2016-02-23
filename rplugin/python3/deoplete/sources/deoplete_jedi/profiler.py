class Colors(object):
    RED = '\033[1;41m'
    GREEN = '\033[1;42m'
    YELLOW = '\033[1;43m'
    BLUE = '\033[1;44m'
    MAGENTA = '\033[1;45m'
    CYAN = '\033[1;46m'
    ENDC = '\033[0m'


def timeit(logger, fmt, threshold):
    from json import dumps
    import time

    def is_json(json_data):
        try:
            json_object = dumps(json_data, indent=4)
        except ValueError:
            return False
        else:
            return json_object

    def timereald(method):
        def timed(*args, **kw):
            start = time.clock()
            result = method(*args, **kw)
            end = time.clock()

            try:
                obj, value = args
                data = is_json(value) if False else value
            except Exception:
                data = is_json(args) if False else args

            sec = (end - start)
            sec_color = Colors.RED
            if sec <= threshold[0]:
                sec_color = Colors.BLUE
            elif sec <= threshold[1]:
                sec_color = Colors.GREEN

            if fmt == 'simple':
                logger.debug("\nName: %r\nClock: %s%2.8f%s sec\n" %
                             (method.__name__,
                              sec_color,
                              sec,
                              Colors.ENDC,
                              ))
            elif fmt == 'verbose':
                logger.debug(
                    "\nName: %r\nClock: %s%2.8f%s sec\nObj: %s\nkw: %s\n%s\n" %
                    (method.__name__,
                     sec_color,
                     sec,
                     Colors.ENDC,
                     obj,
                     kw,
                     data,
                     ))
            return result
        return timed
    return timereald
