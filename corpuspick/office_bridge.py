"""Runs only in the bundled LibreOffice Python, inside the parent's process job.

Protocol: newline JSON requests on stdin; numeric/status-only replies on stdout.
No TCP listener, document text, exception messages, or library logs are returned.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    # The parent assigns this process to its job before releasing this gate.
    if sys.stdin.readline().strip() != 'ready':
        return
    program, profile, pipe = sys.argv[1:]
    dll_directory = os.add_dll_directory(program) if os.name == 'nt' else None
    sys.path.insert(0, program)
    import uno
    import unohelper
    from com.sun.star.task import XInteractionHandler, XInteractionAbort

    class AbortInteraction(unohelper.Base, XInteractionHandler):
        def handle(self, request):
            for continuation in request.getContinuations():
                if isinstance(continuation, XInteractionAbort):
                    continuation.select()
                    return

    def prop(name, value):
        result = uno.createUnoStruct('com.sun.star.beans.PropertyValue')
        result.Name, result.Value = name, value
        return result

    def reply(value):
        print(json.dumps(value), flush=True)

    office = subprocess.Popen([
        str(Path(program) / 'soffice.com'), '-env:UserInstallation=' + Path(profile).as_uri(),
        '--headless', '--nologo', '--nodefault', '--nofirststartwizard', '--norestore',
        '--accept=pipe,name=' + pipe + ';urp;StarOffice.ComponentContext',
    ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    desktop = None
    try:
        local = uno.getComponentContext()
        resolver = local.ServiceManager.createInstanceWithContext('com.sun.star.bridge.UnoUrlResolver', local)
        deadline = time.monotonic() + 18
        while True:
            try:
                context = resolver.resolve('uno:pipe,name=' + pipe + ';urp;StarOffice.ComponentContext')
                break
            except Exception:
                if office.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('startup')
                time.sleep(.05)
        desktop = context.ServiceManager.createInstanceWithContext('com.sun.star.frame.Desktop', context)
        reply({'ready': True, 'office_pid': office.pid})
        for line in sys.stdin:
            request = json.loads(line)
            if request.get('stop'):
                break
            document = None
            result = {'ok': False}
            try:
                document = desktop.loadComponentFromURL(
                    uno.systemPathToFileUrl(request['source']), '_blank', 0,
                    (prop('Hidden', True), prop('ReadOnly', True), prop('Silent', True),
                     prop('MacroExecutionMode', uno.getConstantByName('com.sun.star.document.MacroExecMode.NEVER_EXECUTE')),
                     prop('UpdateDocMode', uno.getConstantByName('com.sun.star.document.UpdateDocMode.NO_UPDATE')),
                     prop('PickListEntry', False), prop('InteractionHandler', AbortInteraction())))
                if document is not None:
                    if request.get('action') == 'stats':
                        pages = document.getRendererCount(document, ())
                        figures = document.getGraphicObjects().getCount()
                        tables = document.getTextTables().getCount()
                        result = {'ok': True, 'stats': {'pages': int(pages),
                                                        'figures': int(figures),
                                                        'tables': int(tables)}}
                    else:
                        document.storeToURL(uno.systemPathToFileUrl(request['output']),
                                            (prop('FilterName', request['filter']), prop('Overwrite', True),
                                             prop('FilterData', uno.Any('[]com.sun.star.beans.PropertyValue',
                                                                       (prop('PageRange', '1'),)))))
                        result = {'ok': True}
            except Exception:
                pass
            finally:
                if document is not None:
                    try:
                        document.close(True)
                    except Exception:
                        # Do not reuse a process with an unclosed document.
                        reply({'ok': False, 'reset': True})
                        return
            reply(result)
    finally:
        if desktop is not None:
            try:
                desktop.terminate()
            except Exception:
                pass
        if office.poll() is None:
            try:
                office.wait(timeout=2)
            except subprocess.TimeoutExpired:
                office.terminate()
        if dll_directory is not None:
            dll_directory.close()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Exception strings can contain a document's path/content.
        print('{"ok":false,"reset":true}', flush=True)
