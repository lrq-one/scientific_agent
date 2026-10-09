"""Scientific semantic scope checks, separate from the unchanged SQL safety guard."""
from __future__ import annotations
import re
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import build_scope, Scope
from app.models.schemas import QueryScope


class UnverifiedScope(ValueError):
    """The conservative checker cannot establish this query's population."""

    def __init__(self, reason):
        super().__init__("UNVERIFIED_SCOPE: " + reason)


class ScopeViolation(ValueError):
    """The SQL provably contradicts an authorized QueryScope."""

    def __init__(self, reason):
        super().__init__("SCOPE_VIOLATION: " + reason)


def validate_scope(sql, params, scope: QueryScope, schema):
    if not any((scope.dataset_version, scope.split, scope.whole_dataset, scope.comparison_target, scope.all_versions, scope.filters)):
        return {"verified": True, "query_scope": scope.model_dump(mode="json"),
                "effective_query_scope": scope.model_dump(mode="json")}
    tree = sqlglot.parse_one(re.sub(r"%\(([A-Za-z_]\w*)\)s", r":\1", sql), read="postgres")
    root = build_scope(tree)
    if root is None:
        raise UnverifiedScope("cannot verify this query structure")
    if any(join.args.get("side") or str(join.args.get("kind", "")).upper() == "CROSS"
           or join.args.get("on") is None and join.args.get("using") is None
           for join in tree.find_all(exp.Join)):
        raise UnverifiedScope("outer/cross/implicit joins require population proof beyond this checker")
    if any(tree.find_all(exp.Not)):
        raise UnverifiedScope("negated predicates cannot establish a positive scope binding")
    user_filters = {k: v for k, v in scope.filters.items() if k not in {"version_bound_run_ids", "version_bound_run_labels"}}
    unsupported_filters = {k for k, v in user_filters.items() if not re.fullmatch(r"[A-Za-z_]\w*", k)
                           or not isinstance(v, (str, int, float, bool))}
    if unsupported_filters:
        raise UnverifiedScope("filter proof is not implemented for " + ", ".join(sorted(unsupported_filters)))
    reachable, seen = [], set()
    def visit(s):
        if id(s) in seen: return
        seen.add(id(s)); reachable.append(s)
        for _, source in s.selected_sources.values():
            if isinstance(source, Scope): visit(source)
        for child in s.subquery_scopes + s.union_scopes: visit(child)
    visit(root)
    def value(node):
        while isinstance(node, (exp.Cast, exp.Paren)): node = node.this
        if isinstance(node, exp.Placeholder): return params.get(node.name)
        if isinstance(node, exp.Literal): return node.this
        return None
    predicates = []
    run_predicates = []
    all_version_predicates = []
    version_scopes = set()
    for s in reachable:
        for node in s.expression.find_all(exp.In, exp.EQ):
            if node.find_ancestor(exp.Select) is not s.expression or node.find_ancestor(exp.Or, exp.Case, exp.Filter): continue
            if not node.find_ancestor(exp.Where, exp.Join): continue
            column = node.this
            if isinstance(node, exp.EQ) and not isinstance(column, exp.Column) and isinstance(node.expression, exp.Column):
                column = node.expression
                bound_nodes = [node.this]
            else:
                bound_nodes = node.expressions if isinstance(node, exp.In) else [node.expression]
            if not isinstance(column, exp.Column): continue
            source = s.sources.get(column.table)
            table = source.name if isinstance(source, exp.Table) else None
            if not column.table:
                owners = [src.name for src in s.sources.values() if isinstance(src, exp.Table) and
                    column.name in {c['name'] for c in schema.get(src.name, [])}]
                table = owners[0] if len(owners)==1 else None
            is_version = column.name in {"dataset_version", "dataset_version_id"} or (
                table == "dataset_versions" and column.name in {"id", "version", "version_name", "label"})
            column_join = isinstance(node, exp.EQ) and isinstance(node.this, exp.Column) and isinstance(node.expression, exp.Column)
            if scope.all_versions and is_version and not column_join:
                bound = [value(x) for x in bound_nodes]
                expected = set(scope.authorized_version_ids if column.name in {"id", "dataset_version_id"}
                               else scope.authorized_version_labels)
                if not expected or any(v is None for v in bound) or set(map(str, bound)) != expected:
                    raise ScopeViolation("All-version population must bind exactly the authorized version set")
                all_version_predicates.append((table, column.name, node, s))
                version_scopes.add(id(s.expression))
            if (table == 'model_runs' and column.name in {'id', 'run_name'}) or (table == 'predictions' and column.name == 'model_run_id'):
                bound = [value(x) for x in bound_nodes]
                aliases = {column.table} if column.table else {alias for alias, src in s.sources.items()
                           if isinstance(src, exp.Table) and src.name == table}
                run_predicates.append((column.name, bound, s, aliases))
        for equality in s.expression.find_all(exp.EQ):
            # Do not let an irrelevant nested SELECT or a bypassing OR prove
            # the outer population constraint. SQLGuard owns security separately.
            if equality.find_ancestor(exp.Select) is not s.expression: continue
            if equality.find_ancestor(exp.Or): continue
            if not equality.find_ancestor(exp.Where, exp.Join, exp.Case, exp.Filter): continue
            a, b = equality.this, equality.expression
            if not isinstance(a, exp.Column): a, b = b, a
            if not isinstance(a, exp.Column): continue
            source = s.sources.get(a.table) if a.table else None
            table = source.name if isinstance(source, exp.Table) else None
            if table is None and not a.table:
                owners = [src.name for src in s.sources.values() if isinstance(src, exp.Table) and
                          a.name in {c["name"] for c in schema.get(src.name, [])}]
                if len(owners) == 1: table = owners[0]
            if table:
                predicates.append((table, a.name, value(b), equality))
    def connected_population(s, anchor_sources):
        edges = {name: set() for name in s.sources}
        for equality in s.expression.find_all(exp.EQ):
            if equality.find_ancestor(exp.Select) is not s.expression or equality.find_ancestor(exp.Or, exp.Not):
                continue
            if not equality.find_ancestor(exp.Where, exp.Join):
                continue
            left, right = equality.this, equality.expression
            if isinstance(left, exp.Column) and isinstance(right, exp.Column) and left.table in edges and right.table in edges:
                edges[left.table].add(right.table)
                edges[right.table].add(left.table)
        reached = set(anchor_sources)
        pending = list(reached)
        while pending:
            for item in edges.get(pending.pop(), ()) - reached:
                reached.add(item)
                pending.append(item)
        return reached

    values = set(scope.authorized_version_ids + scope.authorized_version_labels)
    ids, labels = set(), set()
    if scope.all_versions and not all_version_predicates:
        raise UnverifiedScope("all-version population lacks an explicit authorized version set predicate")
    if scope.all_versions:
        inspected_nodes = {id(node) for _, _, node, _ in all_version_predicates}
        for s in reachable:
            for column in s.expression.find_all(exp.Column):
                if column.find_ancestor(exp.Select) is not s.expression or not column.find_ancestor(exp.Where):
                    continue
                source = s.sources.get(column.table)
                table = source.name if isinstance(source, exp.Table) else None
                version_column = column.name in {"dataset_version", "dataset_version_id"} or (
                    table == "dataset_versions" and column.name in {"id", "version", "version_name", "label"})
                if not version_column:
                    continue
                parent = column.parent
                if isinstance(parent, exp.EQ) and isinstance(parent.this, exp.Column) and isinstance(parent.expression, exp.Column):
                    continue  # inspected equijoin, not a narrowing constant
                if id(parent) not in inspected_nodes:
                    raise UnverifiedScope("additional version restriction cannot prove the all-version population")
    if scope.dataset_version:
        values = {scope.dataset_version}
        if scope.dataset_version_id: values.add(scope.dataset_version_id)
        ids = set(scope.filters.get('version_bound_run_ids', []))
        labels = set(scope.filters.get('version_bound_run_labels', []))
        good = False
        for table, col, bound, node in predicates:
            if node.find_ancestor(exp.Case, exp.Filter): continue
            typ = next((str(c.get("type", "")).lower() for c in schema.get(table, []) if c["name"] == col), "")
            if "uuid" in typ and bound == scope.dataset_version:
                raise ScopeViolation("QueryScope version label was bound to a UUID column; use the metadata ID or join the version label table")
            is_version = col in {"dataset_version", "dataset_version_id"} or (
                table == "dataset_versions" and col in {"id", "version", "version_name", "label"})
            if is_version and str(bound) in values:
                good = True
                version_scopes.add(id(node.find_ancestor(exp.Select)))
        if not good:
            # These lists are server-resolved authorized identity facts, never
            # inferred from arbitrary SQL params or model-supplied labels.
            for col,bound,s,_ in run_predicates:
                if bool(bound) and all(v is not None for v in bound) and set(map(str,bound)) == (labels if col=='run_name' else ids) and bool(ids):
                    good = True
                    version_scopes.add(id(s.expression))
        if not good: raise ScopeViolation("QueryScope missing requested dataset version predicate")
    if scope.split:
        if not any(col == "split" and bound == scope.split and not node.find_ancestor(exp.Case, exp.Filter)
                   for _, col, bound, node in predicates):
            raise ScopeViolation(f"QueryScope missing population split={scope.split!r}; EXPLAIN/SQL success is insufficient")
    split_values = {bound for _, col, bound, _ in predicates if col == 'split' and bound in {'train','validation','test'}}
    population_splits = {bound for _, col, bound, node in predicates if col == 'split' and not node.find_ancestor(exp.Case, exp.Filter)}
    def is_population(table):
        columns={c['name'] for c in schema.get(table.name,[])}
        # Scientific entity rows remain a population even without a split
        # column (e.g. a standalone molecular feature count). Identity/version
        # dimension tables must not become population proof themselves.
        return table.name=='training_molecules' or bool(columns & {'split','molecule_id','structure_type','smiles'})
    populations = [s for s in reachable if any(isinstance(t,exp.Table) and is_population(t)
        for t in s.sources.values())]
    if scope.dataset_version or scope.all_versions:
        def source_is_version_bound(s, visiting):
            if id(s.expression) in version_scopes: return True
            if id(s) in visiting: return False
            visiting = visiting | {id(s)}
            # FROM/JOIN lineage can constrain a population; an independent
            # scalar count or sibling UNION branch cannot constrain it.
            return any(source_is_version_bound(src, visiting) for _,src in s.selected_sources.values() if isinstance(src,Scope))
        for population in populations:
            bound = source_is_version_bound(population,set())
            if not bound:
                tables = sorted(t.name for t in population.sources.values() if isinstance(t,exp.Table))
                raise UnverifiedScope(f"QueryScope population branch {tables} lacks requested dataset version binding; a sibling count cannot prove this branch's scope")
            anchors = {alias for alias, src in population.sources.items()
                       if isinstance(src, Scope) and source_is_version_bound(src, set())}
            for table, col, bound_value, node in predicates:
                if id(node.find_ancestor(exp.Select)) == id(population.expression) and (
                    col in {"dataset_version", "dataset_version_id"}
                    or table == "dataset_versions" and col in {"id", "version", "version_name", "label"}) and str(bound_value) in values:
                    anchors.update(alias for alias, src in population.sources.items()
                                   if isinstance(src, exp.Table) and src.name == table)
            for table, col, node, version_scope in all_version_predicates:
                if version_scope is population:
                    anchors.update(alias for alias, src in population.sources.items()
                                   if isinstance(src, exp.Table) and src.name == table)
            for col, bound_values, run_scope, aliases in run_predicates:
                expected = labels if col == 'run_name' else ids
                if run_scope is population and bound_values and set(map(str, bound_values)) == expected and ids:
                    anchors.update(aliases)
            population_aliases = {alias for alias, src in population.sources.items()
                                  if isinstance(src, exp.Table) and is_population(src)}
            if anchors and not population_aliases <= connected_population(population, anchors):
                raise UnverifiedScope("version-bound source is not connected to every population by inspected equijoins")
    for field, expected in user_filters.items():
        matching = [(table, node) for table, col, bound, node in predicates if col == field and
                    str(bound) == str(expected) and not node.find_ancestor(exp.Case, exp.Filter)]
        def filter_bound(s, visiting):
            if id(s) in visiting:
                return False
            local = {alias for table, node in matching if node.find_ancestor(exp.Select) is s.expression
                     for alias, source in s.sources.items() if isinstance(source, exp.Table) and source.name == table}
            local.update(alias for alias, source in s.sources.items() if isinstance(source, Scope) and
                         filter_bound(source, visiting | {id(s)}))
            population_aliases = {alias for alias, src in s.sources.items() if isinstance(src, exp.Table) and is_population(src)}
            return bool(local) and population_aliases <= connected_population(s, local)
        if not populations or any(not filter_bound(s, set()) for s in populations):
            raise UnverifiedScope(f"population filter {field}={expected!r} cannot be proven on every branch")
    restricted = {id(node.find_ancestor(exp.Select)) for _, col, _, node in predicates if col == "split" and not node.find_ancestor(exp.Case, exp.Filter)}
    def inherited_split(s, visiting=None):
        visiting = set() if visiting is None else visiting
        if id(s) in visiting: return False
        if id(s.expression) in restricted: return True
        return any(inherited_split(src, visiting | {id(s)}) for _, src in s.selected_sources.values()
                   if isinstance(src, Scope))
    whole_covered = bool(populations) and any(not inherited_split(s) for s in populations)
    if scope.split:
        matching = {id(node.find_ancestor(exp.Select)) for _, col, bound, node in predicates
                    if col == "split" and bound == scope.split}
        def matching_split(s, visiting=None):
            visiting = set() if visiting is None else visiting
            if id(s) in visiting: return False
            if id(s.expression) in matching: return True
            return any(matching_split(src, visiting | {id(s)}) for _, src in s.selected_sources.values()
                       if isinstance(src, Scope))
        for population in populations:
            if not matching_split(population):
                raise UnverifiedScope("a sibling population's split cannot prove this branch's split")
    if scope.whole_dataset:
        # Whole + train comparison may legitimately contain a train subquery or
        # UNION branch. Require an actual unrestricted population branch too.
        if not whole_covered:
            allowed_parts = set(scope.comparison_target) & {'train','validation','test'}
            if not population_splits or not population_splits <= allowed_parts:
                raise ScopeViolation("QueryScope whole dataset must not be globally restricted to a split")
    grouped_split = any(c.name == 'split' for group in tree.find_all(exp.Group) for c in group.find_all(exp.Column))
    coverage = {'whole_dataset':whole_covered, 'splits':sorted(split_values), 'grouped_split':grouped_split}
    effective_scope = scope.model_copy(deep=True)
    if scope.whole_dataset and not whole_covered:
        effective_scope.whole_dataset = False
        effective_scope.split = next(iter(population_splits)) if len(population_splits)==1 else None
        effective_scope.comparison_target = sorted(population_splits) if len(population_splits)>1 else []
    return {"verified": True, "query_scope": scope.model_dump(mode="json"), "sql_scope_checked": True,
            'effective_query_scope':effective_scope.model_dump(mode='json'),
            'scope_coverage':coverage, 'partial_scope':bool(scope.whole_dataset and not whole_covered)}


def missing_population_coverage(state, observations):
    """IDs alone are not proof: require exact scope, actual execution, and rows."""
    if not state.populations:
        if state.requires_population_binding:
            return ["unbound_population_requirements"]
        return missing_scope_coverage(state.query_scope, observations)
    missing = []
    for population in state.populations:
        covered = any(result.success and result.data not in (None, []) and
            result.metadata.get("population_id") == population.population_id and
            result.metadata.get("scope_validation", {}).get("verified") is True and
            result.metadata.get("scope_validation", {}).get("effective_query_scope") == population.query_scope.model_dump(mode="json") and
            not result.metadata.get("scope_validation", {}).get("partial_scope")
            for result in observations)
        if not covered:
            missing.append(population.population_id)
    return missing


def missing_scope_coverage(scope, observations):
    """Union actual executed populations; one allowed branch is not the whole goal."""
    coverage = [o.metadata.get('scope_validation', {}).get('scope_coverage', {}) for o in observations if o.success]
    required_splits = set(scope.comparison_target) & {'train','validation','test'}
    covered_splits = {s for c in coverage for s in c.get('splits', [])}
    if any(c.get('grouped_split') and c.get('whole_dataset') for c in coverage): covered_splits |= required_splits
    missing = []
    if scope.whole_dataset and not any(c.get('whole_dataset') for c in coverage): missing.append('whole_dataset')
    missing += sorted(required_splits - covered_splits)
    return missing
