"""Pipelines are trees of steps built by Python modules.

Every step owns its behavior and configuration: a name, a plain-text
description, an optional model and prompts, an optional selector, parameters
and, for composite steps, children. ``describe()`` returns a standardized,
diffable descriptor of the whole subtree, including full prompt texts and the
hashes of the source files that implement each step. ``run(cases, ctx)`` does
the work and calls models only through ``ctx``.

Leaf steps:

- ``ModelStep`` builds one request per group, calls the model, validates the
  answer and updates the group's target cases. It may continue the previous
  step's conversation within the same group.
- ``CodeStep`` updates cases with code and makes no calls.

Composite steps:

- ``Sequence`` runs its children in order over the same cases.
- ``Grouped`` forms groups with its selector and runs its children in order
  within each group, so they share members, targets and a conversation.

Trees are values: ``replace``, ``remove`` and ``insert`` return new trees and
leave the original untouched, so an experiment can modify a base pipeline.
"""
import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import inspect
import json
from pathlib import Path
import re

from pipeline import selectors
from pipeline.cases import Group

ROOT = Path(__file__).resolve().parent.parent
NAME = re.compile(r'[a-z0-9][a-z0-9_-]*\Z')
FRAMEWORK = {'pipeline.steps'}


def sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


def relative(path):
    path = Path(path).resolve()
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


class Model:
    """A model from ``pipeline/models.json``, its credential prefix and request settings."""

    def __init__(self, name, env='APPEALS', **settings):
        self.name, self.env, self.settings = name, env, dict(settings)

    def spec(self):
        return {'model': self.name, 'env': self.env}

    def with_settings(self, **settings):
        return Model(self.name, self.env, **{**self.settings, **settings})

    def describe(self):
        from pipeline.providers import catalog
        entry = catalog().get(self.name, {})
        return {'model': self.name, 'provider': entry.get('provider'), 'env': self.env,
                'settings': dict(self.settings), 'pricing': entry.get('pricing_usd_per_million')}


class Prompt:
    """Prompt text, usually loaded from a file next to the step that uses it."""

    def __init__(self, text, file=None):
        self.text, self.file = text, file

    @classmethod
    def load(cls, path):
        return cls(Path(path).read_text(), relative(path))

    def describe(self):
        return {'file': self.file, 'sha256': sha256(self.text), 'text': self.text}


def prompts_in(folder, *names):
    """Load ``NAME.md`` files from a folder as prompts."""
    return {name: Prompt.load(Path(folder)/f'{name}.md') for name in names}


class Step:
    description = ''
    reads, writes, fields = (), (), ()  # Values read and written; result facts affected.
    max_group = None                    # The largest group the step can handle; None is unlimited.
    code = ()                           # Other repository files its behavior depends on.
    prompt_names = ()                   # Prompts the step needs.
    defaults = {}                       # Parameter defaults.
    composite = False

    def __init__(self, name, *, description=None, children=(), selector=None, model=None, prompts=None,
                 continues=False, **params):
        self.name = name
        if description is not None:
            self.description = description
        self.children = tuple(children)
        self.selector, self.model, self.continues = selector, model, continues
        self.prompts = dict(prompts or {})
        unknown = set(params) - set(self.defaults)
        if unknown:
            raise TypeError(f'{type(self).__name__} has no parameters {", ".join(sorted(unknown))}')
        self.params = {**self.defaults, **params}

    def __repr__(self):
        return f'<{type(self).__name__} {self.name}>'

    # Copies ---------------------------------------------------------------
    def with_(self, **changes):
        """A copy with attributes replaced; ``params`` and ``prompts`` merge."""
        step = copy.copy(self)
        for key, value in changes.items():
            if key in ('params', 'prompts'):
                value = {**getattr(self, key), **value}
            elif key == 'children':
                value = tuple(value)
            elif not hasattr(self, key):
                raise AttributeError(f'{type(self).__name__} has no attribute {key!r}')
            setattr(step, key, value)
        return step

    # Description ----------------------------------------------------------
    def type_name(self):
        cls = type(self)
        return f'{cls.__module__}.{cls.__qualname__}'

    def code_files(self):
        """Hashes of the files implementing this step, excluding the framework."""
        files = set(self.code)
        for cls in type(self).__mro__:
            if cls is object or cls.__module__ in FRAMEWORK:
                continue
            try:
                files.add(relative(inspect.getsourcefile(cls)))
            except TypeError:
                continue
        return {name: sha256((ROOT/name).read_text() if not Path(name).is_absolute() else Path(name).read_text())
                for name in sorted(files)}

    def describe(self, path=''):
        return {'name': self.name, 'path': path, 'type': self.type_name(), 'description': self.description,
                'model': self.model.describe() if self.model else None,
                'prompts': {k: p.describe() for k, p in sorted(self.prompts.items())},
                'conversation': ('continue' if self.continues else 'fresh') if self.model else None,
                'selector': self.selector.describe() if self.selector else None,
                'params': copy.deepcopy(self.params), 'reads': list(self.reads), 'writes': list(self.writes),
                'fields': list(self.fields), 'max_group': self.max_group, 'code': self.code_files(),
                'children': [c.describe(f'{path}/{c.name}' if path else c.name) for c in self.children]}

    # Tree editing ---------------------------------------------------------
    def walk(self, path=''):
        yield path, self
        for child in self.children:
            yield from child.walk(f'{path}/{child.name}' if path else child.name)

    def find(self, path):
        step = self
        for part in path.split('/') if path else []:
            step = next((c for c in step.children if c.name == part), None)
            if step is None:
                raise KeyError(f'No step {path!r}')
        return step

    def _edit(self, parts, edit):
        children = list(self.children)
        index = next((i for i, c in enumerate(children) if c.name == parts[0]), None)
        if index is None:
            raise KeyError(f'No step {parts[0]!r} in {self.name}')
        if len(parts) == 1:
            edit(children, index)
        else:
            children[index] = children[index]._edit(parts[1:], edit)
        return self.with_(children=children)

    def replace(self, path, step):
        """A new tree with the step at ``path`` replaced."""
        return self._edit(path.split('/'), lambda children, i: children.__setitem__(i, step))

    def remove(self, path):
        return self._edit(path.split('/'), lambda children, i: children.pop(i))

    def insert(self, path, step, before=False):
        """A new tree with ``step`` placed after (or before) the step at ``path``."""
        return self._edit(path.split('/'), lambda children, i: children.insert(i if before else i+1, step))

    def update(self, path, **changes):
        """A new tree with the step at ``path`` copied with changes (see ``with_``)."""
        return self.replace(path, self.find(path).with_(**changes))

    # Execution ------------------------------------------------------------
    def run(self, cases, ctx):
        """Work on the cases this step selects, one group at a time."""
        run_groups(self.selector or selectors.each(), [(self, ctx)], cases, ctx)

    def process(self, group, ctx):
        raise NotImplementedError


def run_groups(selector, work, cases, ctx):
    """Form groups and run each ``(step, ctx)`` in order within every group."""
    groups = [Group(members) for members in selector.groups(cases)]

    def one(group):
        for step, step_ctx in work:
            if not group.targets:
                return
            step.process(group, step_ctx)
    ctx.map(one, groups)


class ModelStep(Step):
    """One model call per group: build the request, validate the answer, update targets."""
    max_group = 1

    def request(self, group, conversation, provider):
        """Return ``(request, conversation)``; ``conversation`` is the predecessor's when continuing."""
        raise NotImplementedError

    def accept(self, group, value):
        """Validate an answer without changing any case.

        Raise to reject the whole answer. Otherwise return ``[(case, outcome)]``
        for the targets, where an outcome is a function of the step's path that
        updates the case, or an exception explaining why this case cannot
        accept its part.
        """
        raise NotImplementedError

    def transcript(self, group, value):
        """The assistant turn a continuing step sees for this answer."""
        return json.dumps(value, ensure_ascii=False)

    def process(self, group, ctx):
        provider = ctx.provider(self.model)
        conversation = None
        if self.continues:
            held = group.conversation
            if held is None or held[1] != self.model.name:
                group.fail(ctx.path, 'No conversation from the previous step to continue', 'structure', retryable=False)
                return
            conversation = held[2]
        try:
            request, used = self.request(group, conversation, provider)
        except Exception as exc:
            group.fail(ctx.path, exc, 'request', retryable=False)
            return
        outcomes, value = None, None
        for retry in (False, True):
            value = ctx.call(self, group, provider, request, retry=retry)
            if value is None:
                return  # The group failed or is pending; ctx recorded why.
            try:
                outcomes = self.accept(group, value)
                rejected = [o for _, o in outcomes if isinstance(o, Exception)]
            except Exception as exc:
                outcomes, rejected = exc, [exc]
            ctx.record_acceptance(self, group, request, rejected)
            if not rejected or retry or not ctx.may_retry(self, provider, request):
                break
        if isinstance(outcomes, Exception):
            group.fail(ctx.path, outcomes, 'validation')
            return
        for case, outcome in outcomes:
            if isinstance(outcome, Exception):
                group.fail(ctx.path, outcome, 'validation', cases=[case])
            else:
                outcome(ctx.path)
                if not case.active:
                    group.drop(case)
        if 'conversation' in provider.capabilities and used is not None:
            group.conversation = (ctx.path, self.model.name, used.answer(self.transcript(group, value)))


class CodeStep(Step):
    """Updates cases with code; makes no model calls."""

    def apply(self, case, ctx):
        raise NotImplementedError

    def run(self, cases, ctx):
        for case in (self.selector or selectors.each()).eligible(cases):
            self._apply(case, ctx)

    def process(self, group, ctx):
        for case in list(group.targets):
            self._apply(case, ctx)
            if not case.active:
                group.drop(case)

    def _apply(self, case, ctx):
        try:
            self.apply(case, ctx)
            case.note(ctx.path, event='code')
        except Exception as exc:
            case.fail(ctx.path, exc, 'code', retryable=False)


class Sequence(Step):
    """Runs its children in order over the same cases."""
    composite = True
    description = 'Runs its steps in order.'

    def run(self, cases, ctx):
        for child in self.children:
            child.run(cases, ctx.at(child))

    def process(self, group, ctx):
        for child in self.children:
            if not group.targets:
                return
            child.process(group, ctx.at(child))


class Grouped(Step):
    """Groups cases with its selector and runs its children within each group.

    With ``wait`` set, nothing runs while any case stopped earlier for a reason
    a retry could change, so retried cases rejoin the same groups.
    """
    composite = True
    defaults = {'wait': False}

    def run(self, cases, ctx):
        if self.params['wait']:
            blocking = [c for c in cases if c.blocking]
            if blocking:
                for case in self.selector.eligible(cases):
                    case.pend(ctx.path, f'Waiting for {len(blocking)} cases that stopped at an earlier step')
                return
        run_groups(self.selector, [(c, ctx.at(c)) for c in self.children], cases, ctx)

    def process(self, group, ctx):
        self.run(group.targets, ctx)


def per_case(name, children, description=None):
    return Grouped(name, children=children, selector=selectors.each(),
                   description=description or 'Runs its steps for one case at a time.')


def validate(root):
    """Raise ValueError listing every structural problem in a tree."""
    from pipeline.providers import PROVIDERS, catalog
    models, problems = catalog(), []

    def check(step, path, grouped):
        where = path or root.name
        names = [c.name for c in step.children]
        if len(names) != len(set(names)):
            problems.append(f'{where}: child names must be unique')
        if not NAME.fullmatch(step.name):
            problems.append(f'{where}: step names use lowercase letters, numbers, dashes and underscores')
        if step.composite and not step.children:
            problems.append(f'{where}: a composite step needs children')
        if not step.composite and step.children:
            problems.append(f'{where}: only composite steps have children')
        missing = [n for n in step.prompt_names if n not in step.prompts]
        if missing:
            problems.append(f'{where}: missing prompts {", ".join(missing)}')
        if isinstance(step, ModelStep):
            entry = models.get(step.model.name) if step.model else None
            if entry is None:
                problems.append(f'{where}: needs a model listed in pipeline/models.json')
            elif entry.get('provider') not in PROVIDERS:
                problems.append(f'{where}: unknown provider type {entry.get("provider")!r}')
            elif 'pricing_usd_per_million' not in entry:
                problems.append(f'{where}: model {step.model.name!r} has no price, so its spending cannot be capped')
        if not step.composite and step.max_group is not None and step.selector \
                and (step.selector.max_cases or float('inf')) > step.max_group:
            problems.append(f'{where}: its selector forms groups larger than {step.max_group}')
        if isinstance(step, Grouped):
            limit = step.selector.max_cases or float('inf')
            for _, inner in step.walk():
                if inner is not step and inner.max_group is not None and limit > inner.max_group \
                        and not isinstance(inner, Grouped):
                    problems.append(f'{where}/{inner.name}: groups of {limit} exceed its limit of {inner.max_group}')
        previous = None
        for child in step.children:
            at = f'{where}/{child.name}' if path else child.name
            if getattr(child, 'continues', False):
                if not (grouped or isinstance(step, Grouped)):
                    problems.append(f'{at}: a conversation continues only within a group')
                elif not isinstance(previous, ModelStep):
                    problems.append(f'{at}: nothing to continue; the previous step makes no model call')
                elif previous.model and child.model and previous.model.name != child.model.name:
                    problems.append(f'{at}: a conversation continues only with the same model as {previous.name}')
                else:
                    kind = PROVIDERS.get(models.get(child.model.name, {}).get('provider')) if child.model else None
                    if kind is not None and 'conversation' not in kind.capabilities:
                        problems.append(f'{at}: its provider cannot continue conversations')
            check(child, f'{path}/{child.name}' if path else child.name,
                  isinstance(step, Grouped) or grouped and isinstance(step, Sequence))
            previous = child
    check(root, '', False)
    if problems:
        raise ValueError('Invalid pipeline:\n  '+'\n  '.join(problems))
    return root
