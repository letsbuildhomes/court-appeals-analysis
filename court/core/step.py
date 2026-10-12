"""The step interface, version 1.

A step takes keyed maps in and returns one keyed map out. How it gets there
is its own business. A library entry defines one ``Step`` subclass and
declares:

    interface   the interface version it targets (1)
    inputs      input names and their contracts, e.g. {'cases': 'cases'}; None means unchecked
    output      the contract of its output, or None
    defaults    settings and their defaults; a definition may set only these
    prompts     prompt slots it needs, e.g. ('system', 'instructions')
    children    child slots it expects, or None for any number (generic composites)
    uses        helper library entries it loads with ``self.use(name)``

``run(inputs, ctx)`` receives ``{input name: {key: value or Failure}}`` and
returns ``{key: value or Failure}``. One item failing never stops the others:
return a ``Failure`` for it. A step reaches models, caches, traces and its
children only through ``ctx`` (see ``court.core.context``).
"""
from court.core.datasets import Failure, is_failure

INTERFACE = 1
INTERFACES = (1,)


class Step:
    interface = INTERFACE
    description = ''
    inputs = {'input': None}
    output = None
    defaults = {}
    prompts = ()
    children = ()
    uses = ()

    def __init__(self, node, prompts, children, helpers):
        """Built by the library loader from a resolved node; not called by pipeline code."""
        self.node = node
        self.name = node['name']
        unknown = set(node.get('settings', {})) - set(self.defaults)
        if unknown:
            raise ValueError(f'{node["step"]} has no settings {", ".join(sorted(unknown))}')
        self.settings = {**self.defaults, **node.get('settings', {})}
        self.prompt_texts = dict(prompts)
        self.child_steps = list(children)
        self._helpers = dict(helpers)

    def __repr__(self):
        return f'<{self.node["step"]} at {self.name}>'

    def input_names(self):
        """The input maps this step takes; generic composites read them from settings."""
        return list(self.inputs)

    def prompt(self, slot):
        return self.prompt_texts[slot]

    def use(self, name):
        """A helper entry this step declared in ``uses``."""
        if name not in self._helpers:
            raise KeyError(f'{self.node["step"]} did not declare helper {name}')
        return self._helpers[name]

    def child(self, name):
        for step in self.child_steps:
            if step.name == name:
                return step
        raise KeyError(f'{self.node["step"]} has no child {name}')

    def run(self, inputs, ctx):
        raise NotImplementedError


def passthrough(values):
    """Split a map into ``(working, failed)``: failures pass through untouched."""
    working = {k: v for k, v in values.items() if not is_failure(v)}
    return working, {k: v for k, v in values.items() if is_failure(v)}


__all__ = ['Step', 'Failure', 'is_failure', 'passthrough', 'INTERFACE', 'INTERFACES']
