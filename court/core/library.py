"""The step and prompt libraries: append-only entries loaded by name.

A library name is ``family:variant``. The family is the job and may be
grouped with slashes (``shared/download:standard``); the colon marks the
variant. Files keep plain paths:

    review:legacy                steps/review/legacy.py
    shared/download:standard     steps/shared/download/standard.py
    review:legacy (a prompt)     prompts/review/legacy.md

A step entry defines exactly one ``Step`` subclass. A helper entry defines
functions and no step; it declares the helpers it needs in ``USES`` and loads
them with the injected ``use(name)``. Entries import only ``court.core``,
``court.contracts`` and the standard library or locked dependencies.

Entries are never edited once a pipeline has used them. The loader executes
exactly the bytes it hashes, and refuses a file whose hash differs from the
one a resolved pipeline recorded.
"""
import re
import sys
import threading
import types

from court.config import paths
from court.core.hashing import sha256

NAME = re.compile(r'[a-z0-9][a-z0-9_-]*(/[a-z0-9][a-z0-9_-]*)*:[a-z0-9][a-z0-9._-]*\Z')


class LibraryError(Exception):
    pass


def check_name(name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise LibraryError(f'Invalid library name {name!r}; use family:variant, e.g. review:legacy')
    return name


def relative(name, folder, suffix):
    family, variant = check_name(name).split(':')
    return folder/family/f'{variant}{suffix}'


class Library:
    def __init__(self, root=None):
        p = paths()
        self.steps = (root/'steps') if root else p.steps
        self.prompt_root = (root/'prompts') if root else p.prompts
        self._modules = {}
        self._lock = threading.RLock()

    # Files ---------------------------------------------------------------
    def entry_path(self, name):
        return relative(name, self.steps, '.py')

    def prompt_path(self, name):
        return relative(name, self.prompt_root, '.md')

    def entry_hash(self, name):
        path = self.entry_path(name)
        if not path.exists():
            raise LibraryError(f'No library entry {name} ({path})')
        return sha256(path.read_bytes())

    def prompt(self, name, expected=None):
        """``(text, hash)`` of a prompt, checked against an expected hash."""
        path = self.prompt_path(name)
        if not path.exists():
            raise LibraryError(f'No prompt {name} ({path})')
        data = path.read_bytes()
        digest = sha256(data)
        if expected and digest != expected:
            raise LibraryError(f'Prompt {name} changed after a pipeline used it; add a new variant instead')
        return data.decode(), digest

    def entries(self):
        """Every entry name in the step library."""
        return sorted(_name(p.relative_to(self.steps), '.py') for p in self.steps.rglob('*.py')
                      if p.name != '__init__.py')

    def prompts(self):
        return sorted(_name(p.relative_to(self.prompt_root), '.md') for p in self.prompt_root.rglob('*.md'))

    # Modules -------------------------------------------------------------
    def module(self, name, expected=None):
        """Load an entry as a module, executing exactly the hashed bytes."""
        path = self.entry_path(name)
        if not path.exists():
            raise LibraryError(f'No library entry {name} ({path})')
        data = path.read_bytes()
        digest = sha256(data)
        if expected and digest != expected:
            raise LibraryError(f'Library entry {name} changed after a pipeline used it; add a new variant instead')
        with self._lock:
            if (name, digest) in self._modules:
                return self._modules[(name, digest)]
            module_name = 'library.'+re.sub(r'[^a-z0-9_]', '_', name)+'_'+digest[:8]
            module = types.ModuleType(module_name)
            module.__file__ = str(path)
            module.LIBRARY_NAME, module.LIBRARY_HASH = name, digest
            module.use = self._user(name, _peek_uses(data))
            sys.modules[module_name] = module
            try:
                exec(compile(data, str(path), 'exec'), module.__dict__)
            except Exception:
                sys.modules.pop(module_name, None)
                raise
            self._modules[(name, digest)] = module
            return module

    def _user(self, owner, declared):
        def use(name):
            if name not in declared:
                raise LibraryError(f'{owner} uses {name} without declaring it in USES')
            return self.module(name)
        return use

    def step_class(self, name, expected=None):
        from court.core.step import Step
        module = self.module(name, expected)
        found = [v for v in vars(module).values()
                 if isinstance(v, type) and issubclass(v, Step) and v.__module__ == module.__name__]
        if len(found) != 1:
            raise LibraryError(f'{name} must define exactly one step; it defines {len(found)}')
        return found[0]

    def uses(self, name):
        """Helper entries an entry depends on, directly or through other helpers."""
        from court.core.step import Step
        module = self.module(name)
        steps = [v for v in vars(module).values()
                 if isinstance(v, type) and issubclass(v, Step) and v.__module__ == module.__name__]
        direct = list(steps[0].uses if steps else getattr(module, 'USES', ()))
        found = []
        for helper in direct:
            check_name(helper)
            for nested in [helper, *self.uses(helper)]:
                if nested not in found:
                    found.append(nested)
        return found


def _name(relative_path, suffix):
    parts = list(relative_path.parts)
    variant = parts.pop()[:-len(suffix)]
    return '/'.join(parts)+':'+variant


def _peek_uses(data):
    """A helper's ``USES`` tuple, read without executing the module."""
    import ast
    for node in ast.parse(data).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'USES' for t in node.targets):
            return tuple(ast.literal_eval(node.value))
    return ()
