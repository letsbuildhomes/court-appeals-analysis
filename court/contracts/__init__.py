"""The saved-data contracts: what each dataset kind contains.

Each module defines its kind, a version and ``check(key, value)``, which
returns a list of problems. Stages on both sides of a contract and every
consumer depend on it, so changing one is a deliberate, versioned change.
"""
from importlib import import_module

KINDS = ('documents', 'pages', 'cases', 'results')


def module(kind):
    if kind not in KINDS:
        raise KeyError(f'No contract {kind!r}')
    return import_module(f'court.contracts.{kind}')


def version_of(kind):
    """The contract version for a dataset kind; intermediate kinds have none."""
    return module(kind).VERSION if kind in KINDS else None


def check(kind, key, value):
    """Problems with one item; kinds without a contract are unchecked."""
    if kind not in KINDS:
        return []
    if kind == 'results':
        from court.contracts import results
        problems = results.validate(value)
        if not problems and value['case'] != key:
            problems = [f'{key}: result is for {value["case"]}']
        return problems
    return module(kind).check(key, value)
