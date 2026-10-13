"""Serve the web app: static files and the JSON API."""
from pathlib import Path
import threading

from court.ui.api import Api
from court.web import serve as serve_web

STATIC = Path(__file__).with_name('static')


def routes(api):
    lab = api.lab
    return {
        '/api/home': api.home, '/api/runs': api.runs, '/api/run': api.run, '/api/run/items': api.run_items,
        '/api/run/trace': api.run_trace, '/api/datasets': api.datasets_list, '/api/dataset': api.dataset_view,
        '/api/item': api.item, '/api/source': api.source, '/api/page-image': api.page_image,
        '/api/page-text': api.page_text, '/api/pipelines': api.pipelines_list, '/api/pipeline': api.pipeline,
        '/api/lab/catalog': lab.catalog, '/api/lab/overview': lab.overview, '/api/lab/cases': lab.cases,
        '/api/lab/case': lab.detail, '/api/lab/source': lab.source,
        '/api/reference': api.reference_list, '/api/reference/case': api.reference_case,
        '/api/reference/source': api.reference_source,
    }


def static():
    files = {'/'+p.name: p for p in STATIC.iterdir() if p.suffix in ('.js', '.css', '.html')}
    return {**files, '/': STATIC/'index.html'}


def warm(api):
    """Compute production's quality profile, the slowest view, before anyone asks."""
    try:
        api.lab.overview('production')
    except Exception:
        pass


def serve(host='0.0.0.0', port=8771, allowed_hosts=(), password=None, public=None):
    """Serve the app; with ``password`` (a ``court.web.Password``) every page needs it.

    ``public`` (a ``court.web.PublicUrl``) is the address a reverse proxy serves it at, if any.
    """
    api = Api()
    threading.Thread(target=warm, args=(api,), daemon=True).start()
    serve_web('Court review', routes(api), static(), host, port, '/', allowed_hosts, password, public)
