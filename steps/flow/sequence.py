"""Run child steps in order, wiring each one's inputs by name.

Settings:
    inputs   the names of the maps this sequence takes (default ["input"])
    output   the child whose output is the sequence's output (default: the last)

A child node's ``inputs`` maps each of its input names to a source: the name
of an earlier sibling (its output) or ``input:NAME`` (one of the sequence's
own inputs). Without ``inputs``, the first child receives the sequence's
inputs with matching names, and each later child receives the previous
child's output as its first input.
"""
from court.core.step import Step


class Sequence(Step):
    description = 'Runs its steps in order; each step\'s inputs are named outputs of earlier steps.'
    inputs = None
    children = None
    defaults = {'inputs': ['input'], 'output': None}

    def input_names(self):
        return list(self.settings['inputs'])

    @classmethod
    def check(cls, node):
        problems, seen = [], []
        own = (node.get('settings') or {}).get('inputs', ['input'])
        for child in node.get('children', []):
            for source in (child.get('inputs') or {}).values():
                if source.startswith('input:'):
                    if source[6:] not in own:
                        problems.append(f'{child["name"]} reads {source}, which this sequence does not take')
                elif source not in seen:
                    problems.append(f'{child["name"]} reads {source}, which is not an earlier step')
            seen.append(child['name'])
        output = (node.get('settings') or {}).get('output')
        if output and output not in seen:
            problems.append(f'output {output} is not a step in this sequence')
        if not node.get('children'):
            problems.append('a sequence needs steps')
        return problems

    def run(self, inputs, ctx):
        outputs, previous = {}, None
        for index, child in enumerate(self.child_steps):
            wiring = child.node.get('inputs')
            if wiring:
                given = {name: inputs[source[6:]] if source.startswith('input:') else outputs[source]
                         for name, source in wiring.items()}
            elif index == 0:
                given = {name: inputs[name] for name in child.input_names() if name in inputs}
            else:
                given = {child.input_names()[0]: previous}
            previous = outputs[child.name] = ctx.run(child, given)
        return outputs[self.settings['output']] if self.settings['output'] else previous
