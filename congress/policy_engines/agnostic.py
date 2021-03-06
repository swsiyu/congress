# Copyright (c) 2013 VMware, Inc. All rights reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#
from congress.datalog.base import ACTION_POLICY_TYPE
from congress.datalog.base import DATABASE_POLICY_TYPE
from congress.datalog.base import MATERIALIZED_POLICY_TYPE
from congress.datalog.base import NONRECURSIVE_POLICY_TYPE
from congress.datalog.base import StringTracer
from congress.datalog.base import Tracer
from congress.datalog import compile
from congress.datalog.compile import Event
from congress.datalog.database import Database
from congress.datalog.materialized import MaterializedViewTheory
from congress.datalog.nonrecursive import ActionTheory
from congress.datalog.nonrecursive import NonrecursiveRuleTheory
from congress.datalog import unify
from congress.datalog.utility import iterstr
from congress.dse import deepsix
from congress.exception import CongressException
from congress.exception import DanglingReference
from congress.exception import PolicyException
from congress.openstack.common import log as logging

LOG = logging.getLogger(__name__)


class ExecutionLogger(object):
    def __init__(self):
        self.messages = []

    def debug(self, msg, *args):
        self.messages.append(msg % args)

    def info(self, msg, *args):
        self.messages.append(msg % args)

    def warn(self, msg, *args):
        self.messages.append(msg % args)

    def error(self, msg, *args):
        self.messages.append(msg % args)

    def critical(self, msg, *args):
        self.messages.append(msg % args)

    def content(self):
        return '\n'.join(self.messages)

    def empty(self):
        self.messages = []


def list_to_database(atoms):
    database = Database()
    for atom in atoms:
        if atom.is_atom():
            database.insert(atom)
    return database


def string_to_database(string, theories=None):
    return list_to_database(compile.parse(
        string, theories=theories))


##############################################################################
# Runtime
##############################################################################

class Trigger(object):
    """A chunk of code that should be run when a table's contents changes."""

    def __init__(self, tablename, policy, callback, modal=None):
        self.tablename = tablename
        self.policy = policy
        self.callback = callback
        self.modal = modal

    def __str__(self):
        return "Trigger on table=%s; policy=%s; modal=%s with callback %s." % (
            self.tablename, self.policy, self.modal, self.callback)


class TriggerRegistry(object):
    """A collection of triggers and algorithms to analyze that collection."""

    def __init__(self, dependency_graph):
        # graph containing relationships between tables
        self.dependency_graph = dependency_graph

        # set of triggers that are currently registered
        self.triggers = set()

        # map from table to triggers relevant to changes for that table
        self.index = {}

    def register_table(self, tablename, policy, callback, modal=None):
        """Register CALLBACK to run when TABLENAME changes."""
        # TODO(thinrichs): either fix dependency graph to differentiate
        #   between execute[alice:p] and alice:p or reject rules
        #   in which both occur
        trigger = Trigger(tablename, policy, callback, modal=modal)
        self.triggers.add(trigger)
        self._add_indexes(trigger)
        LOG.info("registered trigger: %s", trigger)
        return trigger

    def unregister(self, trigger):
        """Unregister trigger ID."""
        self.triggers.remove(trigger)
        self._delete_indexes(trigger)

    def update_dependencies(self, dependency_graph_changes=None):
        """Inform registry of changes to the dependency graph.

        Changes are accounted for in self.dependency_graph, but
        by giving the list of changes we can avoid recomputing
        all dependencies from scratch.
        """
        # TODO(thinrichs): instead of destroying the index and
        #   recomputing from scratch, look at the changes and
        #   figure out the delta.
        self.index = {}
        for trigger in self.triggers:
            self._add_indexes(trigger)

    def _add_indexes(self, trigger):
        full_table = compile.Tablename.build_service_table(
            trigger.policy, trigger.tablename)
        deps = self.dependency_graph.dependencies(full_table)
        if deps is None:
            deps = set([full_table])
        for table in deps:
            if table in self.index:
                self.index[table].add(trigger)
            else:
                self.index[table] = set([trigger])

    def _delete_indexes(self, trigger):
        full_table = compile.Tablename.build_service_table(
            trigger.policy, trigger.tablename)
        deps = self.dependency_graph.dependencies(full_table)
        if deps is None:
            deps = set([full_table])
        for table in deps:
            self.index[table].discard(trigger)

    def relevant_triggers(self, events):
        """Return the set of triggers that are relevant to the EVENTS.

        Each EVENT may either be a compile.Event or a tablename.
        """
        table_changes = set()
        for event in events:
            if isinstance(event, compile.Event):
                if compile.is_rule(event.formula):
                    table_changes |= set(
                        [lit.table.global_tablename(event.target)
                         for lit in event.formula.heads])
                else:
                    table_changes.add(
                        event.formula.table.global_tablename(event.target))
            elif isinstance(event, basestring):
                table_changes.add(event)
        triggers = set()
        for table in table_changes:
            if table in self.index:
                triggers |= self.index[table]
        return triggers

    def _index_string(self):
        """Build string representation of self.index; useful for debugging."""
        s = '{'
        s += ";".join(["%s -> %s" % (key, ",".join(str(x) for x in value))
                       for key, value in self.index.iteritems()])
        s += '}'
        return s

    @classmethod
    def triggers_by_table(cls, triggers):
        """Return dictionary from tables to triggers."""
        d = {}
        for trigger in triggers:
            table = (trigger.tablename, trigger.policy, trigger.modal)
            if table not in d:
                d[table] = [trigger]
            else:
                d[table].append(trigger)
        return d


class Runtime (object):
    """Runtime for the Congress policy language.

    Only have one instantiation in practice, but using a
    class is natural and useful for testing.
    """

    def __init__(self):
        # tracer object
        self.tracer = Tracer()
        # record execution
        self.logger = ExecutionLogger()
        # collection of theories
        self.theory = {}
        # collection of builtin theories
        self.builtin_policy_names = set()
        # dependency graph for all theories
        self.global_dependency_graph = (
            compile.RuleDependencyGraph())
        # triggers
        self.trigger_registry = TriggerRegistry(self.global_dependency_graph)
        # execution triggers
        self.execution_triggers = {}

    def create_policy(self, name, abbr=None, kind=None, id=None):
        """Create a new policy and add it to the runtime.

        ABBR is a shortened version of NAME that appears in
        traces.  KIND is the name of the datastructure used to
        represent a policy.
        """
        if not isinstance(name, basestring):
            raise KeyError("Policy name %s must be a string" % name)
        if name in self.theory:
            raise KeyError("Policy with name %s already exists" % name)
        if not isinstance(abbr, basestring):
            abbr = name[0:5]
        LOG.debug("Creating policy <%s> with abbr <%s> and kind <%s>",
                  name, abbr, kind)
        if kind is None:
            kind = NONRECURSIVE_POLICY_TYPE
        else:
            kind = kind.lower()
        if kind == NONRECURSIVE_POLICY_TYPE:
            PolicyClass = NonrecursiveRuleTheory
        elif kind == ACTION_POLICY_TYPE:
            PolicyClass = ActionTheory
        elif kind == DATABASE_POLICY_TYPE:
            PolicyClass = Database
        elif kind == MATERIALIZED_POLICY_TYPE:
            PolicyClass = MaterializedViewTheory
        else:
            raise PolicyException(
                "Unknown kind of policy: %s" % kind)
        policy_obj = PolicyClass(name=name, abbr=abbr, theories=self.theory)
        policy_obj.set_id(id)
        policy_obj.set_tracer(self.tracer)
        self.theory[name] = policy_obj
        LOG.debug("Created policy <%s> with abbr <%s> and kind <%s>",
                  policy_obj.name, policy_obj.abbr, policy_obj.kind)
        return policy_obj

    def delete_policy(self, name, disallow_dangling_refs=False):
        """Deletes policy with name NAME or throws KeyError or DanglingRefs."""
        LOG.debug("Deleting policy named %s", name)
        if name not in self.theory:
            raise KeyError("Policy with name %s does not exist" % name)
        if disallow_dangling_refs:
            refs = self._references_to_policy(name)
            if refs:
                refmsg = ";".join("%s: %s" % (policy, rule)
                                  for policy, rule in refs)
                raise DanglingReference(
                    "Cannot delete %s because it would leave dangling "
                    "references: %s" % (name, refmsg))
        # delete the rules explicitly so cross-theory state is properly
        #   updated
        events = [compile.Event(formula=rule, insert=False, target=name)
                  for rule in self.theory[name].content()]
        permitted, errs = self.update(events)
        if not permitted:
            # This shouldn't happen
            msg = ";".join(str(x) for x in errs)
            LOG.exception("%s:: failed to empty theory %s: %s",
                          self.name, name, msg)
            raise PolicyException("Policy %s could not be deleted since "
                                  "rules could not all be deleted: %s",
                                  name, msg)
        del self.theory[name]

    def rename_policy(self, oldname, newname):
        """Renames policy OLDNAME to NEWNAME or raises KeyError."""
        if newname in self.theory:
            raise KeyError('Cannot rename %s to %s: %s already exists',
                           oldname, newname, newname)
        try:
            self.theory[newname] = self.theory[oldname]
            del self.theory[oldname]
        except KeyError:
            raise KeyError('Cannot rename %s to %s: %s does not exist',
                           oldname, newname, oldname)

    # TODO(thinrichs): make Runtime act like a dictionary so that we
    #   can iterate over policy names (keys), check if a policy exists, etc.
    def policy_exists(self, name):
        """Returns True iff policy called NAME exists."""
        return name in self.theory

    def policy_names(self):
        """Returns list of policy names."""
        return self.theory.keys()

    def policy_object(self, name=None, id=None):
        """Return policy by given name.  Raises KeyError if does not exist."""
        assert name or id
        if name:
            try:
                if not id or str(self.theory[name].id) == str(id):
                    return self.theory[name]
            except KeyError:
                raise KeyError("Policy with name %s and id %s does not "
                               "exist" % (name, str(id)))
        elif id:
            for n in self.policy_names():
                if str(self.theory[n].id) == str(id):
                    return self.theory[n]
            raise KeyError("Policy with name %s and id %s does not "
                           "exist" % (name, str(id)))

    def policy_type(self, name):
        """Return type of policy NAME.  Throws KeyError if does not exist."""
        return self.policy_object(name).kind

    def get_target(self, name):
        if name is None:
            if len(self.theory) == 1:
                name = next(self.theory.iterkeys())
            elif len(self.theory) == 0:
                raise PolicyException("No policies exist.")
            else:
                raise PolicyException(
                    "Must choose a policy to operate on")
        if name not in self.theory:
            raise PolicyException("Unknown policy " + str(name))
        return self.theory[name]

    def get_target_name(self, name):
        """Resolve NAME to the name of a proper policy (even if it is None).

        Raises PolicyException there is no such policy.
        """
        return self.get_target(name).name

    def get_action_names(self, target):
        """Return a list of the names of action tables."""
        if target not in self.theory:
            return []
        actionth = self.theory[target]
        actions = actionth.select(self.parse1('action(x)'))
        return [action.arguments[0].name for action in actions]

    def table_log(self, table, msg, *args):
        self.tracer.log(table, "RT    : %s" % msg, *args)

    def set_tracer(self, tracer):
        if isinstance(tracer, Tracer):
            self.tracer = tracer
            for th in self.theory:
                self.theory[th].set_tracer(tracer)
        else:
            self.tracer = tracer[0]
            for th, tracr in tracer[1].items():
                if th in self.theory:
                    self.theory[th].set_tracer(tracr)

    def get_tracer(self):
        """Return (Runtime's tracer, dict of tracers for each theory).

        Useful so we can temporarily change tracing.
        """
        d = {}
        for th in self.theory:
            d[th] = self.theory[th].get_tracer()
        return (self.tracer, d)

    def debug_mode(self):
        tracer = Tracer()
        tracer.trace('*')
        self.set_tracer(tracer)

    def production_mode(self):
        tracer = Tracer()
        self.set_tracer(tracer)

    # External interface

    def set_schema(self, name, schema, complete=False):
        """Set the schema for module NAME to be SCHEMA."""
        self.theory[name].schema = compile.Schema(schema, complete=complete)

    def select(self, query, target=None, trace=False):
        """Event handler for arbitrary queries.

        Returns the set of all instantiated QUERY that are true.
        """
        if isinstance(query, basestring):
            return self._select_string(query, self.get_target(target), trace)
        elif isinstance(query, tuple):
            return self._select_tuple(query, self.get_target(target), trace)
        else:
            return self._select_obj(query, self.get_target(target), trace)

    def initialize_tables(self, tablenames, facts, target=None):
        """Event handler for (re)initializing a collection of tables

        @facts must be an iterable containing compile.Fact objects.
        """
        target_theory = self.get_target(target)
        alltables = set([compile.Tablename.build_service_table(
                         target_theory.name, x)
                         for x in tablenames])
        triggers = self.trigger_registry.relevant_triggers(alltables)
        LOG.info("relevant triggers (init): %s",
                 ";".join(str(x) for x in triggers))
        # run queries on relevant triggers *before* applying changes
        table_triggers = self.trigger_registry.triggers_by_table(triggers)
        table_data_old = self._compute_table_contents(table_triggers)
        # actually apply the updates
        target_theory.initialize_tables(tablenames, facts)
        # rerun the trigger queries to check for changes
        table_data_new = self._compute_table_contents(table_triggers)
        # run triggers if tables changed
        for table, triggers in table_triggers.iteritems():
            if table_data_old[table] != table_data_new[table]:
                for trigger in triggers:
                    trigger.callback(table,
                                     table_data_old[table],
                                     table_data_new[table])

    def insert(self, formula, target=None):
        """Event handler for arbitrary insertion (rules and facts)."""
        if isinstance(formula, basestring):
            return self._insert_string(formula, target)
        elif isinstance(formula, tuple):
            return self._insert_tuple(formula, target)
        else:
            return self._insert_obj(formula, target)

    def delete(self, formula, target=None):
        """Event handler for arbitrary deletion (rules and facts)."""
        if isinstance(formula, basestring):
            return self._delete_string(formula, target)
        elif isinstance(formula, tuple):
            return self._delete_tuple(formula, target)
        else:
            return self._delete_obj(formula, target)

    def update(self, sequence, target=None):
        """Event handler for applying an arbitrary sequence of insert/deletes.

        If TARGET is supplied, it overrides the targets in SEQUENCE.
        """
        if isinstance(sequence, basestring):
            return self._update_string(sequence, target)
        else:
            return self._update_obj(sequence, target)

    def policy(self, target=None):
        """Event handler for querying policy."""
        target = self.get_target(target)
        if target is None:
            return ""
        return " ".join(str(p) for p in target.policy())

    def content(self, target=None):
        """Event handler for querying content()."""
        target = self.get_target(target)
        if target is None:
            return ""
        return " ".join(str(p) for p in target.content())

    def simulate(self, query, theory, sequence, action_theory, delta=False,
                 trace=False):
        """Event handler for simulation.

        The computation of a query given an action sequence. That sequence
        can include updates to atoms, updates to rules, and action
        invocations.  Returns a collection of Literals (as a string if the
        query and sequence are strings or as a Python collection otherwise).
        If delta is True, the return is a collection of Literals where
        each tablename ends with either + or - to indicate whether
        that fact was added or deleted.
        Example atom update: q+(1) or q-(1)
        Example rule update: p+(x) :- q(x) or p-(x) :- q(x)
        Example action invocation:
           create_network(17), options:value(17, "name", "net1") :- true
        """
        assert self.get_target(theory) is not None, "Theory must be known"
        assert self.get_target(action_theory) is not None, (
            "Action theory must be known")
        if isinstance(query, basestring) and isinstance(sequence, basestring):
            return self._simulate_string(query, theory, sequence,
                                         action_theory, delta, trace)
        else:
            return self._simulate_obj(query, theory, sequence, action_theory,
                                      delta, trace)

    def tablenames(self, body_only=False):
        """Return tablenames occurring in some theory."""
        tables = set()
        for th in self.theory.values():
            tables |= set(th.tablenames(body_only=body_only))
        return tables

    def reserved_tablename(self, name):
        return name.startswith('___')

    def table_contents_queries(self, tablename, policy, modal=None):
        """Return list of queries yielding contents of TABLENAME in POLICY."""
        # TODO(thinrichs): Handle case of multiple arities.  Connect to API.
        arity = self.arity(tablename, policy, modal)
        if arity is None:
            return
        args = ["x" + str(i) for i in xrange(0, arity)]
        atom = tablename + "(" + ",".join(args) + ")"
        if modal is None:
            return [atom]
        else:
            return [modal + "[" + atom + "]"]

    def register_trigger(self, tablename, callback, policy=None, modal=None):
        """Register CALLBACK to run when table TABLENAME changes."""
        # calling self.get_target_name to check if policy actually exists
        #   and to resolve None to a policy name
        return self.trigger_registry.register_table(
            tablename, self.get_target_name(policy), callback, modal=modal)

    def unregister_trigger(self, trigger):
        """Unregister CALLBACK for table TABLENAME."""
        return self.trigger_registry.unregister(trigger)

    def arity(self, table, theory, modal=None):
        """Return number of columns for TABLE in THEORY.

        TABLE can include the policy name. <policy>:<table>
        THEORY is the name of the theory we are asking.
        MODAL is the value of the modal, if any.
        """
        arity = self.get_target(theory).arity(table, modal)
        if arity is not None:
            return arity
        policy, tablename = compile.Tablename.parse_service_table(table)
        if policy not in self.theory:
            return
        return self.theory[policy].arity(tablename, modal)

    def find_subpolicy(self, required_tables, prohibited_tables,
                       output_tables, target=None):
        """Return a subset of rules in @theory.

        @required_tables is the set of tablenames that a rule must depend on.
        @prohibited_tables is the set of tablenames that a rule must
        NOT depend on.
        @output_tables is the set of tablenames that all rules must support.
        """
        target = self.get_target(target)
        if target is None:
            return
        subpolicy = compile.find_subpolicy(
            target.content(),
            required_tables,
            prohibited_tables,
            output_tables)
        return " ".join(str(p) for p in subpolicy)

    # Internal interface
    # Translate different representations of formulas into
    #   the compiler's internal representation and then invoke
    #   appropriate theory's version of the API.

    # Arguments that are strings are suffixed with _string.
    # All other arguments are instances of Theory, Literal, etc.

    ###################################
    # Update policies and data.

    # insert: convenience wrapper around Update
    def _insert_string(self, policy_string, theory_string):
        policy = self.parse(policy_string)
        return self._update_obj(
            [Event(formula=x, insert=True, target=theory_string)
             for x in policy],
            theory_string)

    def _insert_tuple(self, iter, theory_string):
        return self._insert_obj(compile.Literal.create_from_iter(iter),
                                theory_string)

    def _insert_obj(self, formula, theory_string):
        return self._update_obj([Event(formula=formula, insert=True,
                                       target=theory_string)],
                                theory_string)

    # delete: convenience wrapper around Update
    def _delete_string(self, policy_string, theory_string):
        policy = self.parse(policy_string)
        return self._update_obj(
            [Event(formula=x, insert=False, target=theory_string)
             for x in policy],
            theory_string)

    def _delete_tuple(self, iter, theory_string):
        return self._delete_obj(compile.Literal.create_from_iter(iter),
                                theory_string)

    def _delete_obj(self, formula, theory_string):
        return self._update_obj([Event(formula=formula, insert=False,
                                       target=theory_string)],
                                theory_string)

    # update
    def _update_string(self, events_string, theory_string):
        assert False, "Not yet implemented--need parser to read events"

    def _update_obj(self, events, theory_string):
        """Do the updating.

        Checks if applying EVENTS is permitted and if not
        returns a list of errors.  If it is permitted, it
        applies it and then returns a list of changes.
        In both cases, the return is a 2-tuple (if-permitted, list).
        Note: All event.target fields are the NAMES of theories, not
        theory objects.
        """
        # TODO(thinrichs): look into whether we can move the bulk of the
        # trigger code into Theory, esp. so that MaterializedViewTheory
        # can implement it more efficiently.
        self.table_log(None, "Updating with %s", iterstr(events))
        errors = []
        # resolve event targets and check that they actually exist
        for event in events:
            if event.target is None:
                event.target = theory_string
            try:
                event.target = self.get_target_name(event.target)
            except PolicyException as e:
                errors.append(e)
        if len(errors) > 0:
            return (False, errors)
        # eliminate noop events
        events = self._actual_events(events)
        if not len(events):
            return (True, [])
        # check that the updates would not cause an error
        by_theory = self.group_events_by_target(events)
        for th, th_events in by_theory.iteritems():
            th_obj = self.get_target(th)
            errors.extend(th_obj.update_would_cause_errors(th_events))
        # update dependency graph (and undo it if errors)
        graph_changes = self.global_dependency_graph.formula_update(
            events, include_atoms=False)
        if graph_changes:
            if self.global_dependency_graph.has_cycle():
                # TODO(thinrichs): include path
                errors.append(PolicyException(
                    "Rules are recursive"))
                self.global_dependency_graph.undo_changes(graph_changes)
        if len(errors) > 0:
            return (False, errors)
        # modify execution triggers
        self._maintain_triggers()
        # figure out relevant triggers
        triggers = self.trigger_registry.relevant_triggers(events)
        LOG.info("relevant triggers (update): %s",
                 ";".join(str(x) for x in triggers))
        # signal trigger registry about graph updates
        self.trigger_registry.update_dependencies(graph_changes)

        # run queries on relevant triggers *before* applying changes
        table_triggers = self.trigger_registry.triggers_by_table(triggers)
        table_data_old = self._compute_table_contents(table_triggers)
        # actually apply the updates
        changes = []
        for th, th_events in by_theory.iteritems():
            changes.extend(self.get_target(th).update(events))
        # rerun the trigger queries to check for changes
        table_data_new = self._compute_table_contents(table_triggers)
        # run triggers if tables changed
        for table, triggers in table_triggers.iteritems():
            if table_data_old[table] != table_data_new[table]:
                for trigger in triggers:
                    trigger.callback(table,
                                     table_data_old[table],
                                     table_data_new[table])
        # return non-error and the list of changes
        return (True, changes)

    def _maintain_triggers(self):
        pass

    def _actual_events(self, events):
        actual = []
        for event in events:
            th_obj = self.get_target(event.target)
            actual.extend(th_obj.actual_events([event]))
        return actual

    def _compute_table_contents(self, table_policy_pairs):
        data = {}   # dict from (table, policy) to set of query results
        for table, policy, modal in table_policy_pairs:
            th = self.get_target(policy)
            queries = self.table_contents_queries(table, policy, modal) or []
            data[(table, policy, modal)] = set()
            for query in queries:
                ans = set(self._select_obj(self.parse1(query), th, False))
                data[(table, policy, modal)] |= ans
        return data

    def group_events_by_target(self, events):
        """Return mapping of targets and events.

        Return a dictionary mapping event.target to the list of events
        with that target.  Assumes each event.target is a string.
        Returns a dictionary from event.target to <list of events>.
        """
        by_target = {}
        for event in events:
            if event.target not in by_target:
                by_target[event.target] = [event]
            else:
                by_target[event.target].append(event)
        return by_target

    def _reroute_events(self, events):
        """Events re-routing.

        Given list of events with different event.target values,
        change each event.target so that the events are routed to the
        proper place.
        """
        by_target = self.group_events_by_target(events)
        for target, target_events in by_target.items():
            newth = self._compute_route(target_events, target)
            for event in target_events:
                event.target = newth

    def _references_to_policy(self, name):
        refs = []
        name = name + ":"
        for th_obj in self.theory.itervalues():
            for rule in th_obj.policy():
                if any(table.startswith(name) for table in rule.tablenames()):
                    refs.append((name, rule))
        return refs

    ##########################
    # Analyze (internal) state

    # select
    def _select_string(self, policy_string, theory, trace):
        policy = self.parse(policy_string)
        assert (len(policy) == 1), (
            "Queries can have only 1 statement: {}".format(
                [str(x) for x in policy]))
        results = self._select_obj(policy[0], theory, trace)
        if trace:
            return (compile.formulas_to_string(results[0]), results[1])
        else:
            return compile.formulas_to_string(results)

    def _select_tuple(self, tuple, theory, trace):
        return self._select_obj(compile.Literal.create_from_iter(tuple),
                                theory, trace)

    def _select_obj(self, query, theory, trace):
        if trace:
            old_tracer = self.get_tracer()
            tracer = StringTracer()  # still LOG.debugs trace
            tracer.trace('*')     # trace everything
            self.set_tracer(tracer)
            value = set(theory.select(query))
            self.set_tracer(old_tracer)
            return (value, tracer.get_value())
        return set(theory.select(query))

    # simulate
    def _simulate_string(self, query, theory, sequence, action_theory, delta,
                         trace):
        query = self.parse1(query)
        sequence = self.parse(sequence)
        result = self._simulate_obj(query, theory, sequence, action_theory,
                                    delta, trace)
        return compile.formulas_to_string(result)

    def _simulate_obj(self, query, theory, sequence, action_theory, delta,
                      trace):
        """Simulate objects.

        Both THEORY and ACTION_THEORY are names of theories.
        Both QUERY and SEQUENCE are parsed.
        """
        assert compile.is_datalog(query), "Query must be formula"
        # Each action is represented as a rule with the actual action
        #    in the head and its supporting data (e.g. options) in the body
        assert all(compile.is_extended_datalog(x) for x in sequence), (
            "Sequence must be an iterable of Rules")
        th_object = self.get_target(theory)

        if trace:
            old_tracer = self.get_tracer()
            tracer = StringTracer()  # still LOG.debugs trace
            tracer.trace('*')     # trace everything
            self.set_tracer(tracer)

        # if computing delta, query the current state
        if delta:
            self.table_log(query.tablename(),
                           "** Simulate: Querying %s", query)
            oldresult = th_object.select(query)
            self.table_log(query.tablename(),
                           "Original result of %s is %s",
                           query, iterstr(oldresult))

        # apply SEQUENCE
        self.table_log(query.tablename(), "** Simulate: Applying sequence %s",
                       iterstr(sequence))
        undo = self.project(sequence, theory, action_theory)

        # query the resulting state
        self.table_log(query.tablename(), "** Simulate: Querying %s", query)
        result = set(th_object.select(query))
        self.table_log(query.tablename(), "Result of %s is %s", query,
                       iterstr(result))
        # rollback the changes
        self.table_log(query.tablename(), "** Simulate: Rolling back")
        self.project(undo, theory, action_theory)

        # if computing the delta, do it
        if delta:
            result = set(result)
            oldresult = set(oldresult)
            pos = result - oldresult
            neg = oldresult - result
            pos = [formula.make_update(is_insert=True) for formula in pos]
            neg = [formula.make_update(is_insert=False) for formula in neg]
            result = pos + neg
        if trace:
            self.set_tracer(old_tracer)
            return (result, tracer.get_value())
        return result

    # Helpers

    def _react_to_changes(self, changes):
        """Filters changes and executes actions contained therein."""
        # LOG.debug("react to: %s", iterstr(changes))
        actions = self.get_action_names()
        formulas = [change.formula for change in changes
                    if (isinstance(change, Event)
                        and change.is_insert()
                        and change.formula.is_atom()
                        and change.tablename() in actions)]
        # LOG.debug("going to execute: %s", iterstr(formulas))
        self.execute(formulas)

    def _data_listeners(self):
        return [self.theory[self.ENFORCEMENT_THEORY]]

    def _compute_route(self, events, theory):
        """Compute rerouting.

        When a formula is inserted/deleted (in OPERATION) into a THEORY,
        it may need to be rerouted to another theory.  This function
        computes that rerouting.  Returns a Theory object.
        """
        self.table_log(None, "Computing route for theory %s and events %s",
                       theory.name, iterstr(events))
        # Since Enforcement includes Classify and Classify includes Database,
        #   any operation on data needs to be funneled into Enforcement.
        #   Enforcement pushes it down to the others and then
        #   reacts to the results.  That is, we really have one big theory
        #   Enforcement + Classify + Database as far as the data is concerned
        #   but formulas can be inserted/deleted into each policy individually.
        if all([compile.is_atom(event.formula) for event in events]):
            if (theory is self.theory[self.CLASSIFY_THEORY] or
                    theory is self.theory[self.DATABASE]):
                return self.theory[self.ENFORCEMENT_THEORY]
        return theory

    def project(self, sequence, policy_theory, action_theory):
        """Apply the list of updates SEQUENCE.

        Apply the list of updates SEQUENCE, where actions are described
        in ACTION_THEORY. Return an update sequence that will undo the
        projection.

        SEQUENCE can include atom insert/deletes, rule insert/deletes,
        and action invocations.  Projecting an action only
        simulates that action's invocation using the action's description;
        the results are therefore only an approximation of executing
        actions directly.  Elements of SEQUENCE are just formulas
        applied to the given THEORY.  They are NOT Event()s.

        SEQUENCE is really a program in a mini-programming
        language--enabling results of one action to be passed to another.
        Hence, even ignoring actions, this functionality cannot be achieved
        by simply inserting/deleting.
        """
        actth = self.theory[action_theory]
        policyth = self.theory[policy_theory]
        # apply changes to the state
        newth = NonrecursiveRuleTheory(abbr="Temp")
        newth.tracer.trace('*')
        actth.includes.append(newth)
        # TODO(thinrichs): turn 'includes' into an object that guarantees
        #   there are no cycles through inclusion.  Otherwise we get
        #   infinite loops
        if actth is not policyth:
            actth.includes.append(policyth)
        actions = self.get_action_names(action_theory)
        self.table_log(None, "Actions: %s", iterstr(actions))
        undos = []         # a list of updates that will undo SEQUENCE
        self.table_log(None, "Project: %s", sequence)
        last_results = []
        for formula in sequence:
            self.table_log(None, "** Updating with %s", formula)
            self.table_log(None, "Actions: %s", iterstr(actions))
            self.table_log(None, "Last_results: %s", iterstr(last_results))
            tablename = formula.tablename()
            if tablename not in actions:
                if not formula.is_update():
                    raise PolicyException(
                        "Sequence contained non-action, non-update: " +
                        str(formula))
                updates = [formula]
            else:
                self.table_log(tablename, "Projecting %s", formula)
                # define extension of current Actions theory
                if formula.is_atom():
                    assert formula.is_ground(), (
                        "Projection atomic updates must be ground")
                    assert not formula.is_negated(), (
                        "Projection atomic updates must be positive")
                    newth.define([formula])
                else:
                    # instantiate action using prior results
                    newth.define(last_results)
                    self.table_log(tablename, "newth (with prior results) %s",
                                   iterstr(newth.content()))
                    bindings = actth.top_down_evaluation(
                        formula.variables(), formula.body, find_all=False)
                    if len(bindings) == 0:
                        continue
                    grounds = formula.plug_heads(bindings[0])
                    grounds = [act for act in grounds if act.is_ground()]
                    assert all(not lit.is_negated() for lit in grounds)
                    newth.define(grounds)
                self.table_log(tablename,
                               "newth contents (after action insertion): %s",
                               iterstr(newth.content()))
                # self.table_log(tablename, "action contents: %s",
                #     iterstr(actth.content()))
                # self.table_log(tablename, "action.includes[1] contents: %s",
                #     iterstr(actth.includes[1].content()))
                # self.table_log(tablename, "newth contents: %s",
                #     iterstr(newth.content()))
                # compute updates caused by action
                updates = actth.consequences(compile.is_update)
                updates = self.resolve_conflicts(updates)
                updates = unify.skolemize(updates)
                self.table_log(tablename, "Computed updates: %s",
                               iterstr(updates))
                # compute results for next time
                for update in updates:
                    newth.insert(update)
                last_results = actth.consequences(compile.is_result)
                last_results = set([atom for atom in last_results
                                    if atom.is_ground()])
            # apply updates
            for update in updates:
                undo = self.project_updates(update, policy_theory)
                if undo is not None:
                    undos.append(undo)
        undos.reverse()
        if actth is not policyth:
            actth.includes.remove(policyth)
        actth.includes.remove(newth)
        return undos

    def project_updates(self, delta, theory):
        """Project atom/delta rule insertion/deletion.

        Takes an atom/rule DELTA with update head table
        (i.e. ending in + or -) and inserts/deletes, respectively,
        that atom/rule into THEORY after stripping
        the +/-. Returns None if DELTA had no effect on the
        current state.
        """
        theory = delta.theory_name() or theory

        self.table_log(None, "Applying update %s to %s", delta, theory)
        th_obj = self.theory[theory]
        insert = delta.tablename().endswith('+')
        newdelta = delta.drop_update().drop_theory()
        changed = th_obj.update([Event(formula=newdelta, insert=insert)])
        if changed:
            return delta.invert_update()
        else:
            return None

    def resolve_conflicts(self, atoms):
        """If p+(args) and p-(args) are present, removes the p-(args)."""
        neg = set()
        result = set()
        # split atoms into NEG and RESULT
        for atom in atoms:
            if atom.table.table.endswith('+'):
                result.add(atom)
            elif atom.table.table.endswith('-'):
                neg.add(atom)
            else:
                result.add(atom)
        # add elems from NEG only if their inverted version not in RESULT
        for atom in neg:
            if atom.invert_update() not in result:  # slow: copying ATOM here
                result.add(atom)
        return result

    def parse(self, string):
        return compile.parse(string, theories=self.theory)

    def parse1(self, string):
        return compile.parse1(string, theories=self.theory)


##############################################################################
# ExperimentalRuntime
##############################################################################

class ExperimentalRuntime (Runtime):
    def explain(self, query, tablenames=None, find_all=False, target=None):
        """Event handler for explanations.

        Given a ground query and a collection of tablenames
        that we want the explanation in terms of,
        return proof(s) that the query is true. If
        FIND_ALL is True, returns list; otherwise, returns single proof.
        """
        if isinstance(query, basestring):
            return self.explain_string(
                query, tablenames, find_all, self.get_target(target))
        elif isinstance(query, tuple):
            return self.explain_tuple(
                query, tablenames, find_all, self.get_target(target))
        else:
            return self.explain_obj(
                query, tablenames, find_all, self.get_target(target))

    def remediate(self, formula):
        """Event handler for remediation."""
        if isinstance(formula, basestring):
            return self.remediate_string(formula)
        elif isinstance(formula, tuple):
            return self.remediate_tuple(formula)
        else:
            return self.remediate_obj(formula)

    def execute(self, action_sequence):
        """Event handler for execute:

        Execute a sequence of ground actions in the real world.
        """
        if isinstance(action_sequence, basestring):
            return self.execute_string(action_sequence)
        else:
            return self.execute_obj(action_sequence)

    def access_control(self, action, support=''):
        """Event handler for making access_control request.

        ACTION is an atom describing a proposed action instance.
        SUPPORT is any data that should be assumed true when posing
        the query.  Returns True iff access is granted.
        """
        # parse
        if isinstance(action, basestring):
            action = self.parse1(action)
            assert compile.is_atom(action), "ACTION must be an atom"
        if isinstance(support, basestring):
            support = self.parse(support)
        # add support to theory
        newth = NonrecursiveRuleTheory(abbr="Temp")
        newth.tracer.trace('*')
        for form in support:
            newth.insert(form)
        acth = self.theory[self.ACCESSCONTROL_THEORY]
        acth.includes.append(newth)
        # check if action is true in theory
        result = len(acth.select(action, find_all=False)) > 0
        # allow new theory to be freed
        acth.includes.remove(newth)
        return result

    # explain
    def explain_string(self, query_string, tablenames, find_all, theory):
        policy = self.parse(query_string)
        assert len(policy) == 1, "Queries can have only 1 statement"
        results = self.explain_obj(policy[0], tablenames, find_all, theory)
        return compile.formulas_to_string(results)

    def explain_tuple(self, tuple, tablenames, find_all, theory):
        self.explain_obj(compile.Literal.create_from_iter(tuple),
                         tablenames, find_all, theory)

    def explain_obj(self, query, tablenames, find_all, theory):
        return theory.explain(query, tablenames, find_all)

    # remediate
    def remediate_string(self, policy_string):
        policy = self.parse(policy_string)
        assert len(policy) == 1, "Queries can have only 1 statement"
        return compile.formulas_to_string(self.remediate_obj(policy[0]))

    def remediate_tuple(self, tuple, theory):
        self.remediate_obj(compile.Literal.create_from_iter(tuple))

    def remediate_obj(self, formula):
        """Find a collection of action invocations

        That if executed result in FORMULA becoming false.
        """
        actionth = self.theory[self.ACTION_THEORY]
        classifyth = self.theory[self.CLASSIFY_THEORY]
        # look at FORMULA
        if compile.is_atom(formula):
            pass  # TODO(tim): clean up unused variable
            # output = formula
        elif compile.is_regular_rule(formula):
            pass  # TODO(tim): clean up unused variable
            # output = formula.head
        else:
            assert False, "Must be a formula"
        # grab a single proof of FORMULA in terms of the base tables
        base_tables = classifyth.base_tables()
        proofs = classifyth.explain(formula, base_tables, False)
        if proofs is None:  # FORMULA already false; nothing to be done
            return []
        # Extract base table literals that make that proof true.
        #   For remediation, we assume it suffices to make any of those false.
        #   (Leaves of proof may not be literals or may not be written in
        #    terms of base tables, despite us asking for base tables--
        #    because of negation.)
        leaves = [leaf for leaf in proofs[0].leaves()
                  if (compile.is_atom(leaf) and
                      leaf.table in base_tables)]
        self.table_log(None, "Leaves: %s", iterstr(leaves))
        # Query action theory for abductions of negated base tables
        actions = self.get_action_names()
        results = []
        for lit in leaves:
            goal = lit.make_positive()
            if lit.is_negated():
                goal.table = goal.table + "+"
            else:
                goal.table = goal.table + "-"
            # return is a list of goal :- act1, act2, ...
            # This is more informative than query :- act1, act2, ...
            for abduction in actionth.abduce(goal, actions, False):
                results.append(abduction)
        return results

    ##########################
    # Execute actions

    def execute_string(self, actions_string):
        self.execute_obj(self.parse(actions_string))

    def execute_obj(self, actions):
        """Executes the list of ACTION instances one at a time.

        For now, our execution is just logging.
        """
        LOG.debug("Executing: %s", iterstr(actions))
        assert all(compile.is_atom(action) and action.is_ground()
                   for action in actions)
        action_names = self.get_action_names()
        assert all(action.table in action_names for action in actions)
        for action in actions:
            if not action.is_ground():
                if self.logger is not None:
                    self.logger.warn("Unground action to execute: %s", action)
                continue
            if self.logger is not None:
                self.logger.info("%s", action)

##############################################################################
# Engine that operates on the DSE
##############################################################################


def d6service(name, keys, inbox, datapath, args):
    return DseRuntime(name, keys, inbox, datapath, args)


class PolicySubData (object):
    def __init__(self, trigger):
        self.table_trigger = trigger
        self.to_add = ()
        self.to_rem = ()
        self.dataindex = trigger.policy + ":" + trigger.tablename

    def trigger(self):
        return self.table_trigger

    def changes(self):
        result = []
        for row in self.to_add:
            event = Event(formula=row, insert=True)
            result.append(event)
        for row in self.to_rem:
            event = Event(formula=row, insert=False)
            result.append(event)
        return result


class DseRuntime (Runtime, deepsix.deepSix):
    def __init__(self, name, keys, inbox, datapath, args):
        Runtime.__init__(self)
        deepsix.deepSix.__init__(self, name, keys, inbox=inbox,
                                 dataPath=datapath)
        self.msg = None
        self.last_policy_change = None
        self.d6cage = args['d6cage']
        self.rootdir = args['rootdir']
        self.policySubData = {}
        self.log_actions_only = args.get('log_actions_only', False)

    def extend_schema(self, service_name, schema):
        newschema = {}
        for key, value in schema:
            newschema[service_name + ":" + key] = value
        super(DseRuntime, self).extend_schema(self, newschema)

    def receive_msg(self, msg):
        self.log("received msg %s", msg)
        self.msg = msg

    def receive_data(self, msg):
        """Event handler for when a dataservice publishes data.

        That data can either be the full table (as a list of tuples)
        or a delta (a list of Events).
        """
        self.log("received data msg %s", msg)
        # if empty data, assume it is an init msg, since noop otherwise
        if len(msg.body.data) == 0:
            self.receive_data_full(msg)
        else:
            # grab an item from any iterable
            dataelem = iter(msg.body.data).next()
            if isinstance(dataelem, Event):
                self.receive_data_update(msg)
            else:
                self.receive_data_full(msg)

    def receive_data_full(self, msg):
        """Handler for when dataservice publishes full table."""
        self.log("received full data msg for %s: %s",
                 msg.header['dataindex'], iterstr(msg.body.data))
        tablename = msg.header['dataindex']
        service = msg.replyTo

        # Use a generator to avoid instantiating all these Facts at once.
        literals = (compile.Fact(tablename, row) for row in msg.body.data)

        self.initialize_tables([tablename], literals, target=service)
        self.log("full data msg for %s", tablename)

    def receive_data_update(self, msg):
        """Handler for when dataservice publishes a delta."""
        self.log("received update data msg for %s: %s",
                 msg.header['dataindex'], iterstr(msg.body.data))
        events = msg.body.data
        for event in events:
            assert compile.is_atom(event.formula), (
                "receive_data_update received non-atom: " +
                str(event.formula))
            # prefix tablename with data source
            event.target = msg.replyTo
        (permitted, changes) = self.update(events)
        if not permitted:
            raise CongressException(
                "Update not permitted." + '\n'.join(str(x) for x in changes))
        else:
            tablename = msg.header['dataindex']
            service = msg.replyTo
            self.log("update data msg for %s from %s caused %d "
                     "changes: %s", tablename, service, len(changes),
                     iterstr(changes))
            if tablename in self.theory[service].tablenames():
                rows = self.theory[service].content([tablename])
                self.log("current table: %s", iterstr(rows))

    def receive_policy_update(self, msg):
        self.log("received policy-update msg %s",
                 iterstr(msg.body.data))
        # update the policy and subscriptions to data tables.
        self.last_policy_change = self.process_policy_update(msg.body.data)

    def process_policy_update(self, events):
        self.log("process_policy_update %s" % events)
        # body_only so that we don't subscribe to tables in the head
        oldtables = self.tablenames(body_only=True)
        result = self.update(events)
        newtables = self.tablenames(body_only=True)
        self.update_table_subscriptions(oldtables, newtables)
        return result

    def initialize_table_subscriptions(self):
        """Initialize table subscription.

        Once policies have all been loaded, this function subscribes to
        all the necessary tables.  See UPDATE_TABLE_SUBSCRIPTIONS as well.
        """
        self.update_table_subscriptions(set(), self.tablenames())

    def update_table_subscriptions(self, oldtables, newtables):
        """Update table subscription.

        Change the subscriptions from OLDTABLES to NEWTABLES, ensuring
        to load all the appropriate services.
        """
        add = newtables - oldtables
        rem = oldtables - newtables
        self.log("Tables:: Old: %s, new: %s, add: %s, rem: %s",
                 oldtables, newtables, add, rem)
        # subscribe to the new tables (loading services as required)
        for table in add:
            if not self.reserved_tablename(table):
                (service, tablename) = compile.Tablename.parse_service_table(
                    table)
                if service is not None:
                    self.log("Subscribing to new (service, table): (%s, %s)",
                             service, tablename)
                    self.subscribe(service, tablename,
                                   callback=self.receive_data)

        # unsubscribe from the old tables
        for table in rem:
            (service, tablename) = compile.Tablename.parse_service_table(table)
            if service is not None:
                self.log("Unsubscribing to new (service, table): (%s, %s)",
                         service, tablename)
                self.unsubscribe(service, tablename)

    def execute_action(self, service_name, action, action_args):
        """Event handler for action execution.

        :param service_name: openstack service to perform the action on,
        e.g. 'nova', 'neutron'
        :param action: action to perform on service, e.g. an API call
        :param action_args: positional-args and named-args in format:
            {'positional': ['p_arg1', 'p_arg2'],
            'named': {'name1': 'n_arg1', 'name2': 'n_arg2'}}.
        """
        # Log the execution
        LOG.info("%s:: executing: %s:%s on %s",
                 self.name, service_name, action, action_args)
        if self.logger is not None:
            pos_args = ''
            if 'positional' in action_args:
                pos_args = ", ".join(str(x) for x in action_args['positional'])
            named_args = ''
            if 'named' in action_args:
                named_args = ", ".join(
                    "%s=%s" % (key, val)
                    for key, val in action_args['named'].iteritems())
            delimit = ''
            if pos_args and named_args:
                delimit = ', '
            self.logger.info(
                "Executing %s:%s(%s%s%s)",
                service_name, action, pos_args, delimit, named_args)
        if self.log_actions_only:
            return
        # execute the action on a service in the DSE
        service = self.d6cage.service_object(service_name)
        if not service:
            raise PolicyException("Service %s not found" % service_name)
        if not action:
            raise PolicyException("Action not found")
        LOG.info("Sending request(%s:%s), args = %s",
                 service.name, action, action_args)
        self.request(service.name, action, args=action_args)

    def pub_policy_result(self, table, olddata, newdata):
        """Callback for policy table triggers."""
        LOG.debug("grabbing policySubData[%s]", table)
        policySubData = self.policySubData[table]
        policySubData.to_add = newdata - olddata
        policySubData.to_rem = olddata - newdata
        self.log("Table Data:: Old: %s, new: %s, add: %s, rem: %s",
                 olddata, newdata, policySubData.to_add, policySubData.to_rem)

        self.publish(policySubData.dataindex, newdata)

    def subhandler(self, msg):
        """handler for policy table subscription

        when someone subscribes to policy defined tables, register a
        trigger for that table and publish table results when there is
        updates.
        """

        dataindex = msg.header['dataindex']
        (policy, tablename) = compile.Tablename.parse_service_table(dataindex)
        # we only care about policy table subscription
        if policy is None:
            return

        if not (tablename, policy, None) in self.policySubData:
            trig = self.trigger_registry.register_table(tablename,
                                                        policy,
                                                        self.pub_policy_result)
            self.policySubData[(tablename, policy, None)] = PolicySubData(trig)

    def unsubhandler(self, msg):
        """Remove triggers when unsubscribe."""
        dataindex = msg.header['dataindex']
        sender = msg.replyTo
        (policy, tablename) = compile.Tablename.parse_service_table(dataindex)
        if (tablename, policy, None) in self.policySubData:
            # release resource if no one cares about it any more
            subs = self.pubdata[dataindex].getsubscribers()
            # The sender is the last subscriber
            # inunsub() will remove it from pubdata[dataindex] later
            if [sender] == subs.keys():
                sub = self.policySubData.pop((tablename, policy, None))
                self.trigger_registry.unregister(sub.trigger())

        return True

    def prepush_processor(self, data, dataindex, type=None):
        """Called before push.

        Takes as input the DATA that the receiver needs and returns
        the payload for the message. If this is a regular publication
        message, make the payload just the delta; otherwise, make the
        payload the entire table.
        """
        # This routine basically ignores DATA and sends a delta
        # of policy table (i.e. dataindex) changes part of the state.
        self.log("prepush_processor: dataindex <%s> data: %s", dataindex, data)
        # if not a regular publication, just return the original data
        if type != 'pub':
            self.log("prepush_processor: returned original data")
            if type == 'sub' and data is None:
                # Always want to send initialization of []
                return []
            return data
        # grab deltas to publish to subscribers
        (policy, tablename) = compile.Tablename.parse_service_table(dataindex)
        result = self.policySubData[(tablename, policy, None)].changes()
        if len(result) == 0:
            # Policy engine expects an empty update to be an init msg
            # So if delta is empty, return None, which signals
            # the message should not be sent.
            result = None
            text = "None"
        else:
            text = iterstr(result)
        self.log("prepush_processor for <%s> returning with %s items",
                 dataindex, text)
        return result

    def _maintain_triggers(self):
        # ensure there is a trigger registered to execute actions
        curr_tables = set(self.global_dependency_graph.tables_with_modal(
            'execute'))
        # add new triggers
        for table in curr_tables:
            LOG.debug("%s:: checking for missing trigger table %s",
                      self.name, table)
            if table not in self.execution_triggers:
                (policy, tablename) = compile.Tablename.parse_service_table(
                    table)
                LOG.debug("creating new trigger for policy=%s, table=%s",
                          policy, tablename)
                trig = self.trigger_registry.register_table(
                    tablename, policy,
                    lambda table, old, new: self._execute_table(
                        policy, tablename, old, new),
                    modal='execute')
                self.execution_triggers[table] = trig
        # remove triggers no longer needed
        #    Using copy of execution_trigger keys so we can delete inside loop
        for table in self.execution_triggers.keys():
            LOG.debug("%s:: checking for stale trigger table %s",
                      self.name, table)
            if table not in curr_tables:
                LOG.debug("removing trigger for table %s", table)
                try:
                    self.trigger_registry.unregister(
                        self.execution_triggers[table])
                    del self.execution_triggers[table]
                except KeyError:
                    LOG.exception(
                        "Tried to unregister non-existent trigger: %s", table)

    def _execute_table(self, theory, table, old, new):
        # LOG.info("execute_table(theory=%s, table=%s, old=%s, new=%s",
        #          theory, table, ";".join(str(x) for x in old),
        #          ";".join(str(x) for x in new))
        service, tablename = compile.Tablename.parse_service_table(table)
        service = service or theory
        for newlit in new - old:
            args = [term.name for term in newlit.arguments]
            LOG.info("%s:: on service %s executing %s on %s",
                     self.name, service, tablename, args)
            try:
                self.execute_action(service, tablename, {'positional': args})
            except PolicyException as e:
                LOG.error(str(e))
