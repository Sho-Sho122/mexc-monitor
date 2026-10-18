"""Self-contained frozen semantics. Mutable authoring catalog is never a resolver."""
import ast
import copy
import inspect
from functools import lru_cache
from .config import digest, iso, dt


@lru_cache(maxsize=1)
def engine_identity():
    # AST identities exclude paths, comments, line numbers and runtime metadata.
    # Changes to decision implementations invalidate old models instead of silently
    # reusing their evidence. No automatic new-model migration is performed.
    from . import shadow, forward
    from .completeness import population
    from .registry import normalize_model
    from model511 import regime_engine, unique_engine, execution
    from model511 import config as legacy_config, source_reader
    functions = [normalize_model, forward.matches, forward._update, population, shadow.hit, shadow.prices, shadow.evaluate,
                 regime_engine.regime, unique_engine.unique_events,
                 execution.entry_execution, execution.costs, membership,
                 legacy_config.bucket, legacy_config.dt, source_reader.number]
    result = {}
    for fn in functions:
        tree = ast.parse(inspect.getsource(fn))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
                node.body.pop(0)
        result[fn.__module__+'.'+fn.__name__] = digest(ast.dump(tree, include_attributes=False))
    return result


def feature(name, definition, version, available=None):
    semantic = {'name': name, 'definition': definition, 'version': str(version),
                'available_from_jst': available, 'binding': 'immutable_source_event_field'}
    return {**semantic, 'identity': digest(semantic)}


def builtin_features():
    from .registry import BASE_FIELDS
    return {name: feature(name, 'beta1 immutable Unique/Regime event field: '+name, 'beta1-1')
            for name in sorted(BASE_FIELDS)}


def register_feature(state, request, at):
    from .registry import RESERVED
    from .storage import require_research_ready, audit
    require_research_ready(state)
    name = request.get('name')
    if not name or name in RESERVED or name in builtin_features() or not request.get('definition'):
        raise ValueError('Reserved/built-in or invalid feature')
    previous = state['feature_catalog'].get(name)
    if previous:
        if previous.get('definition') == request['definition'] and previous.get('version', '1') == str(request.get('version', '1')):
            return previous  # exact semantic no-op: do not move availability or audit
        raise ValueError('Immutable feature name; register a distinct versioned name')
    row = feature(name, request['definition'], request.get('version', '1'), iso(at))
    state['feature_catalog'][name] = row
    state['feature_schema_version'] = str(int(state['feature_schema_version'])+1)
    audit(state, 'FEATURE_REGISTERED', at, feature_identity=row['identity'], name=name)
    return row


def effective(model, catalog):
    from .registry import normalize_model
    model = normalize_model(model, catalog)
    features = {}
    builtin = builtin_features()
    for cond in model['entry']:
        name = cond['field']
        f = builtin.get(name) or catalog.get(name)
        if not f or f.get('identity') != digest({k:v for k,v in f.items() if k != 'identity'}):
            raise ValueError('Feature semantic snapshot unavailable: '+name)
        features[name] = copy.deepcopy(f)
    # Registry-wide insertion counter is not a model semantic version.
    decision = {k:v for k,v in model.items() if k != 'feature_schema_version'}
    return {'semantic_schema': 1, 'model': decision, 'features': features,
            'engines': engine_identity(), 'population_policy': 'immutable_source_entry_window_available_at_cutoff',
            'execution_settings_policy': 'prospective_per_entry_no_retroactive_fill'}


def model_blocks(c):
    spec = c.get('effective_model_spec')
    if not spec:
        return ['FROZEN_SEMANTICS_UNRESOLVED']
    if digest(spec) != c['model_spec_hash'] or spec['model'] != {k:v for k,v in c['model_spec_json'].items() if k != 'feature_schema_version'}:
        return ['MODEL_HASH_MISMATCH']
    if spec['engines'] != engine_identity():
        return ['SEMANTIC_ENGINE_MISMATCH']
    for f in spec['features'].values():
        if f.get('identity') != digest({k:v for k,v in f.items() if k != 'identity'}):
            return ['FEATURE_IDENTITY_MISMATCH']
    if set(spec['features']) != {c['field'] for c in spec['model']['entry']}:
        return ['FEATURE_SNAPSHOT_INCOMPLETE']
    if not c.get('git_commit_hash') or dt(c['forward_start_jst']) <= dt(c['created_at_jst']):
        return ['MODEL_NOT_FROZEN']
    return []


def membership(c, event, cutoff=None):
    from .forward import matches
    if model_blocks(c):
        return False, ['FROZEN_SEMANTICS_UNRESOLVED']
    eligible, missing = matches(c['model_spec_json'], event)
    for name, f in c['effective_model_spec']['features'].items():
        if f['available_from_jst'] and dt(f['available_from_jst']) > dt(event['entry_time_jst']):
            missing.append(name)
    if cutoff is not None and dt(event['source_cutoff_jst']) > dt(cutoff):
        missing.append('SOURCE_NOT_AVAILABLE_AT_CUTOFF')
    return eligible and not missing, sorted(set(missing))
