"""The interface every pipeline step component implements.

A component turns a unit of work (one case in a per-case phase, or a batch in
a batched phase) into one provider request, then validates the answer and
updates the cases. The engine handles naming, calling, checkpoints, failure
statuses and conversation threading, so components stay small.
"""
import importlib.util
import json
from pathlib import Path

COMPONENTS = {}


def register(cls):
    if cls.name in COMPONENTS and COMPONENTS[cls.name] is not cls:
        raise ValueError(f'Component {cls.name!r} is already registered')
    COMPONENTS[cls.name] = cls
    return cls


def load_file(path):
    """Import an experiment's component file; its classes register themselves."""
    path = Path(path)
    spec = importlib.util.spec_from_file_location(f'pipeline_extra_{path.stem}_{abs(hash(str(path.resolve())))}', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def exact_members(value, key, expected):
    if len(set(expected)) != len(expected) or not isinstance(value, list) or len(value) != len(expected) \
            or {x.get(key) for x in value} != set(expected):
        raise ValueError(f'Response must cover every {key} exactly once')


class Component:
    name = None
    summary = ''
    scope = 'case'              # 'case' or 'batch'
    activity = 'working'        # Shown while the step runs.
    provider_kinds = frozenset({'chat'})
    prompt_roles = ()           # Roles a recipe maps to prompt names.
    fresh_roles = ()            # Roles needed only when starting a fresh conversation.
    reads, writes, facts = (), (), ()
    failure_status = None       # Batched steps: status for cases whose step failed.
    versioned_names = False     # Include the request hash in call names.
    legacy_names = False        # Saved runs may hold this call under its unversioned name.

    def __init__(self, step_id, spec, provider, prompts):
        self.step_id, self.spec, self.provider = step_id, spec, provider
        self.settings = spec['settings']
        self.prompts = prompts

    @classmethod
    def describe(cls):
        return {'component': cls.name, 'summary': cls.summary, 'scope': cls.scope,
                'reads': list(cls.reads), 'writes': list(cls.writes), 'facts': list(cls.facts),
                'provider_kinds': sorted(cls.provider_kinds), 'prompt_roles': list(cls.prompt_roles)}

    def applies(self, unit):
        return True

    def request(self, unit, conversation):
        """Return (request, conversation) for a fresh or continued conversation."""
        raise NotImplementedError

    def before_call(self, client, name, request, unit):
        pass

    def accept(self, unit, value):
        """Per-case steps: validate the answer and update ``unit['result']``."""
        raise NotImplementedError

    def check(self, unit, value):
        """Batched steps: validate the whole answer before applying it per case."""

    def apply(self, unit, case, result, value):
        """Batched steps: update one case; return True if it continues to the next step."""
        raise NotImplementedError

    def transcript(self, unit, value):
        """The assistant turn recorded when a later step continues this conversation."""
        return json.dumps(value, ensure_ascii=False)
