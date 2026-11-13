import os


def mirror(url, response):
    # The 'dummy' sentinel marks responses that were never successfully
    # parsed; there is nothing to mirror in that case
    if not response or response == 'dummy':
        return
    clean_url = url.replace('http://', '').replace('https://', '').rstrip('/')
    parts = clean_url.split('?')[0].split('/')
    root = parts[0]
    webpage = parts[-1]
    parts.remove(root)
    try:
        parts.remove(webpage)
    except ValueError:
        pass
    prefix = root + '_mirror'
    try:
        os.mkdir(prefix)
    except OSError:
        pass
    suffix = ''
    if parts:
        for directory in parts:
            suffix += directory + '/'
            try:
                os.mkdir(prefix + '/' + suffix)
            except OSError:
                pass
    path = prefix + '/' + suffix
    trail = ''
    if '.' not in webpage:
        trail += '.html'
    if webpage == root:
        name = 'index.html'
    else:
        name = webpage
    if len(url.split('?')) > 1:
        trail += '?' + url.split('?')[1]
    # Binary mode because the body is encoded to UTF-8 bytes below
    with open(path + name + trail, 'wb') as out_file:
        out_file.write(response.encode('utf-8'))
