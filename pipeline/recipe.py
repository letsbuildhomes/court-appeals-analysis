"""Recipes: the pipeline as configuration.

A recipe names providers, prompts, steps and phases. Phases run their steps in
order; per-case phases work on one case at a time, batched phases on groups of
cases chosen by a batcher. A step names its component, provider, settings,
prompts, and whether it continues the previous step's conversation.

An experiment is a directory of files composed over a base recipe:

    recipe.json     a fragment deep-merged into the recipe (null removes a key)
    prompts/*.md    replacement or additional prompts, by file name
    components/*.py additional step components

A run stores its composed recipe in ``recipe.json``, prompt texts in
``prompts.json`` and component files in ``components/``. Runs made before
recipes are described by ``legacy_recipe`` from their manifest.
"""
import copy
import hashlib
import json
from pathlib import Path

from pipeline import batching
from pipeline.components import COMPONENTS, load_file
from pipeline.providers import PROVIDERS, catalog

RECIPE_VERSION = 'appeal-recipe-1'
ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT/'recipes/appeals'
PHASE_KINDS = {'per_case': 'case', 'batched': 'batch'}


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def deep_merge(base, overlay):
    """Merge dictionaries recursively; None removes a key; other values replace."""
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load(directory=BASE):
    """A recipe directory's recipe and prompt texts; prompt entries are file paths."""
    directory = Path(directory)
    recipe = json.loads((directory/'recipe.json').read_text())
    prompts = {name: (directory/path).read_text() for name, path in recipe.get('prompts', {}).items()}
    recipe['prompts'] = {name: digest(text) for name, text in prompts.items()}
    return recipe, prompts


def compose(recipe, prompts, overlay):
    """Compose an experiment directory over a recipe; returns recipe, prompts, components, sources."""
    overlay = Path(overlay)
    recipe, prompts = copy.deepcopy(recipe), dict(prompts)
    sources = {'recipe': None, 'prompts': [], 'components': []}
    fragment_path = overlay/'recipe.json'
    if fragment_path.exists():
        fragment = json.loads(fragment_path.read_text())
        for name, path in (fragment.pop('prompts', None) or {}).items():
            prompts[name] = (overlay/path).read_text()
            sources['prompts'].append(name)
        recipe = deep_merge(recipe, fragment)
        sources['recipe'] = 'recipe.json'
    for path in sorted((overlay/'prompts').glob('*.md')):
        prompts[path.stem] = path.read_text()
        sources['prompts'].append(path.stem)
    components = sorted((overlay/'components').glob('*.py'))
    for path in components:
        load_file(path)
        sources['components'].append(path.name)
    recipe['prompts'] = {name: digest(text) for name, text in prompts.items()}
    return recipe, prompts, components, sources


def legacy_recipe(manifest, prompts):
    """The recipe a run made before recipes executed, from its manifest settings."""
    recipe, _ = load(BASE)
    provider = recipe['providers']['default']
    provider['model'] = manifest.get('model')
    provider['settings'] = {'reasoning': manifest.get('reasoning', 'high'),
                            'max_output_tokens': manifest.get('max_output_tokens', 65536)}
    if manifest.get('review_max_output_tokens') is not None:
        for step in ('review', 'duration'):
            recipe['steps'][step]['settings'] = {'max_output_tokens': manifest['review_max_output_tokens']}
    recipe['phases']['finishing']['batch'] = {'kind': 'sequential', 'max_cases': manifest.get('review_batch_size', 2),
                                              'max_chars': manifest.get('review_batch_chars', 200000)}
    recipe['prompts'] = {name: digest(text) for name, text in prompts.items()}
    recipe['legacy'] = True
    return recipe


def for_run(folder, manifest, prompts):
    """A run's recipe, loading any components it carries."""
    folder = Path(folder)
    for path in sorted((folder/'components').glob('*.py')):
        load_file(path)
    path = folder/'recipe.json'
    recipe = json.loads(path.read_text()) if path.exists() else legacy_recipe(manifest, prompts)
    if recipe['prompts'] != {name: digest(text) for name, text in prompts.items()}:
        raise ValueError('Frozen prompts do not match the run recipe')
    return recipe


def freeze(folder, recipe, components=()):
    """Write a composed recipe (and its component files) into a run directory."""
    folder = Path(folder)
    (folder/'recipe.json').write_text(json.dumps(recipe, indent=2, ensure_ascii=False)+'\n')
    for path in components:
        (folder/'components').mkdir(exist_ok=True)
        (folder/'components'/Path(path).name).write_text(Path(path).read_text())


def sha256(recipe):
    return digest(json.dumps(recipe, sort_keys=True, ensure_ascii=False))


def step_spec(recipe, step_id):
    """A step with its provider settings merged under its own settings."""
    step = recipe['steps'][step_id]
    provider = recipe['providers'][step['provider']]
    return {**step, 'settings': {**provider.get('settings', {}), **step.get('settings', {})},
            'context': step.get('context', 'fresh')}


def validate(recipe):
    """Raise ValueError listing every structural problem."""
    problems = []
    prompts = recipe.get('prompts', {})
    models = catalog()
    for name, provider in recipe.get('providers', {}).items():
        entry = models.get(provider.get('model'))
        if entry is None:
            problems.append(f'Provider {name}: model {provider.get("model")!r} has no entry in pipeline/models.json')
        elif entry.get('provider') not in PROVIDERS:
            problems.append(f'Provider {name}: unknown provider type {entry.get("provider")!r}')
    for phase_id in recipe.get('order', []):
        phase = recipe['phases'].get(phase_id)
        if phase is None:
            problems.append(f'Unknown phase {phase_id!r} in order')
            continue
        if phase.get('kind') not in PHASE_KINDS:
            problems.append(f'Phase {phase_id}: kind must be one of {", ".join(PHASE_KINDS)}')
            continue
        if phase['kind'] == 'batched' and phase.get('batch', {}).get('kind', 'sequential') not in batching.BATCHERS:
            problems.append(f'Phase {phase_id}: unknown batcher {phase["batch"].get("kind")!r}')
        previous = None
        for step_id in phase.get('steps', []):
            step = recipe['steps'].get(step_id)
            if step is None:
                problems.append(f'Phase {phase_id}: unknown step {step_id!r}')
                continue
            component = COMPONENTS.get(step.get('component'))
            provider = recipe['providers'].get(step.get('provider'))
            if component is None:
                problems.append(f'Step {step_id}: unknown component {step.get("component")!r}')
                continue
            if component.scope != PHASE_KINDS[phase['kind']]:
                problems.append(f'Step {step_id}: a {component.scope} component cannot run in a {phase["kind"]} phase')
            if provider is None:
                problems.append(f'Step {step_id}: unknown provider {step.get("provider")!r}')
                continue
            kind = PROVIDERS.get(models.get(provider.get('model'), {}).get('provider'))
            if kind is not None and kind.kind not in component.provider_kinds:
                problems.append(f'Step {step_id}: {component.name} needs a {"/".join(sorted(component.provider_kinds))} provider, '
                                f'not {kind.kind}')
            for role in component.prompt_roles:
                name = step.get('prompts', {}).get(role)
                if step.get('context') == 'continue' and role in component.fresh_roles:
                    continue  # A continued conversation already carries it.
                if name is None or name not in prompts:
                    problems.append(f'Step {step_id}: prompt for role {role!r} is missing')
            if step.get('context', 'fresh') == 'continue':
                if previous is None:
                    problems.append(f'Step {step_id}: nothing to continue; it is the first step of {phase_id}')
                elif step.get('provider') != recipe['steps'][previous].get('provider'):
                    problems.append(f'Step {step_id}: a conversation continues only with the same provider as {previous}')
                elif kind is not None and 'conversation' not in kind.capabilities:
                    problems.append(f'Step {step_id}: provider {step.get("provider")} cannot continue conversations')
            elif step.get('context', 'fresh') != 'fresh':
                problems.append(f'Step {step_id}: context must be fresh or continue')
            previous = step_id
    kinds = [recipe['phases'].get(p, {}).get('kind') for p in recipe.get('order', [])]
    if kinds != ['per_case', 'batched']:
        problems.append('Order must be one per_case phase followed by one batched phase')
    if problems:
        raise ValueError('Invalid recipe:\n  '+'\n  '.join(problems))
    return recipe


def describe(recipe):
    """A self-contained description for artifacts and the dashboard."""
    models = catalog()
    phases = []
    for phase_id in recipe['order']:
        phase = recipe['phases'][phase_id]
        steps = []
        for step_id in phase['steps']:
            spec = step_spec(recipe, step_id)
            component = COMPONENTS.get(spec['component'])
            provider = recipe['providers'][spec['provider']]
            entry = models.get(provider['model'], {})
            steps.append({'id': step_id, **(component.describe() if component else {'component': spec['component']}),
                          'provider': {'name': spec['provider'], 'model': provider['model'],
                                       'type': entry.get('provider'), 'pricing': entry.get('pricing_usd_per_million')},
                          'settings': spec['settings'], 'context': spec['context'],
                          'prompts': {role: {'name': name, 'sha256': recipe['prompts'].get(name)}
                                      for role, name in spec.get('prompts', {}).items()}})
        batch = phase.get('batch')
        phases.append({'id': phase_id, 'kind': phase['kind'], 'steps': steps,
                       'batch': {**batch, 'text': batching.describe(batch)} if batch else None})
    return {'schema_version': RECIPE_VERSION, 'name': recipe.get('name'), 'description': recipe.get('description'),
            'legacy': bool(recipe.get('legacy')), 'sha256': sha256(recipe), 'phases': phases}


def resolved(recipe):
    """A recipe with each step's effective settings and context spelled out."""
    value = copy.deepcopy(recipe)
    for step_id in value.get('steps', {}):
        spec = step_spec(recipe, step_id)
        value['steps'][step_id].update(settings=spec['settings'], context=spec['context'])
    for provider in value.get('providers', {}).values():
        provider.pop('settings', None)
    return value


def diff(before, after, path=()):
    """Changed leaves between two recipes, as dotted paths."""
    if isinstance(before, dict) and after is None or before is None and isinstance(after, dict):
        before, after = before or {}, after or {}
    if isinstance(before, dict) and isinstance(after, dict):
        changes = []
        for key in sorted(set(before) | set(after)):
            if key == 'legacy' and not path:
                continue
            changes += diff(before.get(key), after.get(key), path+(key,))
        return changes
    return [] if before == after else [{'path': '.'.join(path), 'base': before, 'value': after}]
