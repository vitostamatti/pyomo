#  ___________________________________________________________________________
#
#  Pyomo: Python Optimization Modeling Objects
#  Copyright 2017 National Technology and Engineering Solutions of Sandia, LLC
#  Under the terms of Contract DE-NA0003525 with National Technology and
#  Engineering Solutions of Sandia, LLC, the U.S. Government retains certain
#  rights in this software.
#  This software is distributed under the 3-clause BSD License.
#  ___________________________________________________________________________

__all__ = [ 'Connection' ]

from pyomo.core.base.component import ActiveComponentData
from pyomo.core.base.indexed_component import (ActiveIndexedComponent,
    UnindexedComponent_set)
from pyomo.core.base.connector import (SimpleConnector, IndexedConnector,
    _ConnectorData)
from pyomo.core.base.var import Var
from pyomo.core.base.constraint import Constraint
from pyomo.core.base.label import alphanum_label_from_name
from pyomo.core.base.misc import apply_indexed_rule
from pyomo.core.base.plugin import (register_component,
    IPyomoScriptModifyInstance, TransformationFactory)
from pyomo.common.plugin import Plugin, implements
from pyomo.common.timing import ConstructionTimer
from pyomo.common.modeling import unique_component_name
from six import iteritems
from weakref import ref as weakref_ref
import logging, sys

logger = logging.getLogger('pyomo.core')


class _ConnectionData(ActiveComponentData):
    """This class defines the data for a single connection."""

    __slots__ = ('_connectors', '_directed', '_expanded_block')

    def __init__(self, component=None, **kwds):
        #
        # These lines represent in-lining of the
        # following constructors:
        #   - ActiveComponentData
        #   - ComponentData
        self._component = weakref_ref(component) if (component is not None) \
                          else None
        self._active = True

        self._connectors = None
        self._directed = None
        self._expanded_block = None
        if len(kwds):
            self.set_value(kwds)

    def __getstate__(self):
        state = super(_ConnectionData, self).__getstate__()
        for i in _ConnectionData.__slots__:
            state[i] = getattr(self, i)
        return state

    # Note: None of the slots on this class need to be edited, so we
    # don't need to implement a specialized __setstate__ method, and
    # can quietly rely on the super() class's implementation.

    def __getattr__(self, name):
        """Returns self.expanded_block.name if it exists"""
        eb = self.expanded_block
        if eb is not None and hasattr(eb, name):
            return getattr(eb, name)
        # Since the base classes don't support getattr, we can just
        # throw the "normal" AttributeError
        raise AttributeError("'%s' object has no attribute '%s'"
                             % (self.__class__.__name__, name))

    @property
    def source(self):
        # directed can be true before construction
        # so make sure connectors is not None
        return self._connectors[0] if self._directed is True and \
            self._connectors is not None else None

    @property
    def destination(self):
        return self._connectors[1] if self._directed is True and \
            self._connectors is not None else None

    @property
    def connectors(self):
        return self._connectors

    @property
    def directed(self):
        return self._directed

    @property
    def expanded_block(self):
        return self._expanded_block

    def set_value(self, vals):
        """
        Set the connector attributes on this connection.

        If these values are being reassigned, note that the defaults
        are still None, so you may need to repass some attributes.
        """
        source = vals.pop("source", None)
        destination = vals.pop("destination", None)
        connectors = vals.pop("connectors", None)

        if len(vals):
            raise ValueError(
                "set_value passed unrecognized keyword arguments:\n\t" +
                "\n\t".join("%s = %s" % (k,v) for k,v in iteritems(vals)))

        self._validate_conns(source, destination, connectors)

        # tuples do not go through add_component
        self._connectors = tuple(connectors) if connectors is not None \
            else (source, destination)
        self._directed = source is not None

    def _validate_conns(self, source, destination, connectors):
        connector_types = set([SimpleConnector, _ConnectorData])
        msg = "Connection %s: " % self.name
        if connectors is not None:
            if source is not None or destination is not None:
                raise ValueError(msg +
                    "cannot specify 'source' or 'destination' "
                    "when using 'connectors' argument.")
            if (type(connectors) not in (list, tuple) or
                len(connectors) != 2):
                raise ValueError(msg +
                    "argument 'connectors' must be list or tuple "
                    "containing exactly 2 Connectors.")
            for c in connectors:
                if type(c) not in connector_types:
                    if type(c) is IndexedConnector:
                        raise ValueError(msg +
                            "found IndexedConnector '%s' in 'connectors', must "
                            "use single Connectors for Connection." % c.name)
                    raise ValueError(msg +
                        "found object '%s' in 'connectors' not "
                        "of type Connector." % str(c))
        else:
            if source is None or destination is None:
                raise ValueError(msg +
                    "must specify both 'source' and 'destination' "
                    "for directed Connection.")
            if type(source) not in connector_types:
                if type(source) is IndexedConnector:
                    raise ValueError(msg +
                        "found IndexedConnector '%s' as source, must use "
                        "single Connectors for Connection." % source.name)
                raise ValueError(msg +
                    "source object '%s' not of type Connector." % str(source))
            if type(destination) not in connector_types:
                if type(destination) is IndexedConnector:
                    raise ValueError(msg +
                        "found IndexedConnector '%s' as destination, must use "
                        "single Connectors for Connection." % destination.name)
                raise ValueError(msg +
                    "destination object '%s' not of type Connector."
                    % str(destination))


class Connection(ActiveIndexedComponent):
    """
    Component used for equating the members of two Connector objects.

    Constructor arguments:
        source          A single Connector for a directed connection
        destination     A single Connector for a directed connection
        connectors      A two-member list or tuple of single Connectors
                            for an undirected connection
        directed        True if directed. Use along with rule to be able to
                            return an implied (source, destination) tuple
        rule            A function that returns either a dictionary of the
                            connection arguments or a two-member iterable
        doc             A text string describing this component
        name            A name for this component

    Public attributes
        source          The source Connector when directed, else None
        destination     The destination Connector when directed, else None
        connectors      A tuple containing both connectors. If directed, this
                            is in the order (source, destination)
        directed        True if directed, False if not
    """

    _ComponentDataClass = _ConnectionData

    def __new__(cls, *args, **kwds):
        if cls != Connection:
            return super(Connection, cls).__new__(cls)
        if not args or (args[0] is UnindexedComponent_set and len(args)==1):
            return SimpleConnection.__new__(SimpleConnection)
        else:
            return IndexedConnection.__new__(IndexedConnection)

    def __init__(self, *args, **kwds):
        source = kwds.pop("source", None)
        destination = kwds.pop("destination", None)
        connectors = kwds.pop("connectors", None)
        self._directed = kwds.pop("directed", None)
        self._rule = kwds.pop('rule', None)
        kwds.setdefault("ctype", Connection)

        super(Connection, self).__init__(*args, **kwds)

        if source is destination is connectors is None:
            self._init_vals = None
        else:
            self._init_vals = dict(
                source=source, destination=destination, connectors=connectors)

    def construct(self, data=None):
        """
        Initialize the Connection
        """
        if __debug__ and logger.isEnabledFor(logging.DEBUG):
            logger.debug("Constructing Connection %s" % self.name)

        if self._constructed:
            return

        timer = ConstructionTimer(self)
        self._constructed = True

        if self._rule is None and self._init_vals is None:
            # No construction rule or values specified
            return
        elif self._rule is not None and self._init_vals is not None:
            raise ValueError(
                "Cannot specify rule along with source/destination/connectors "
                "keywords for connection '%s'" % self.name)

        self_parent = self._parent()

        if not self.is_indexed():
            if self._rule is None:
                tmp = self._init_vals
            else:
                try:
                    tmp = self._rule(self_parent)
                except Exception:
                    err = sys.exc_info()[1]
                    logger.error(
                        "Rule failed when generating values for "
                        "connection %s:\n%s: %s"
                        % (self.name, type(err).__name__, err))
                    raise
            tmp = self._validate_init_vals(tmp)
            self._setitem_when_not_present(None, tmp)
        else:
            if self._init_vals is not None:
                raise IndexError(
                    "Connection '%s': Cannot initialize multiple indices "
                    "of a connection with single connectors" % self.name)
            for idx in self._index:
                try:
                    tmp = apply_indexed_rule(self, self._rule, self_parent, idx)
                except Exception:
                    err = sys.exc_info()[1]
                    logger.error(
                        "Rule failed when generating values for "
                        "connection %s with index %s:\n%s: %s"
                        % (self.name, str(idx), type(err).__name__, err))
                    raise
                tmp = self._validate_init_vals(tmp)
                self._setitem_when_not_present(idx, tmp)
        timer.report()

    def _validate_init_vals(self, vals):
        # returns dict version of vals if not already dict
        if type(vals) is not dict:
            # check that it's a two-member iterable
            conns = None
            if hasattr(vals, "__iter__"):
                # note: every iterable except strings has an __iter__ attribute
                # but strings are not valid anyway
                conns = tuple(vals)
            if conns is None or len(conns) != 2:
                raise ValueError(
                    "Connection rule for '%s' did not return either a "
                    "dict or a two-member iterable." % self.name)
            if self._directed is True:
                vals = {"source": conns[0], "destination": conns[1]}
            else:
                vals = {"connectors": conns}
        elif self._directed is not None:
            # if for some reason they specified directed, check it
            s = vals.get("source", None)
            d = vals.get("destination", None)
            c = vals.get("connectors", None)
            if (((s is not None or d is not None) and self._directed is False)
                or (c is not None and self._directed is True)):
                raise ValueError(
                    "Passed incorrect value for 'directed' for connection "
                    "'%s'. Value is set automatically when using keywords."
                    % self.name)
        return vals

    def _pprint(self):
        """Return data that will be printed for this component."""
        return (
            [("Size", len(self)),
             ("Index", self._index if self.is_indexed() else None),
             ("Active", self.active)],
            iteritems(self),
            ("Connectors", "Directed", "Active"),
            lambda k, v: ["(%s, %s)" % v.connectors if v.connectors is not None
                            else None,
                          v.directed,
                          v.active])

    def SplitFrac(unit, ctns, ctr, etype):
        # create and potentially initialize split fraction variables
        for ctn in ctns:
            ctn.splitfrac = Var()
            bname = ctn.name
            # get name of Connection by chopping off "_expanded"
            # full name since they might be on sub blocks
            cname = bname[:bname.rfind("_expanded")]
            if hasattr(unit, "splitfrac") and cname in unit.splitfrac:
                d = unit.splitfrac[cname]
                if type(d) is dict:
                    val, fix = d["val"], d["fix"]
                else:
                    val, fix = d, True
                ctn.splitfrac = val
                if fix:
                    ctn.splitfrac.fix()

        # create constraints for each extensive variable using splitfrac
        for name, lst in iteritems(ctr.extensives[etype]):
            cname = "%s_split" % name
            var = ctr.vars[name]
            for evar in lst:
                ctn = evar.parent_block()
                def rule(m, *args):
                    if evar.is_indexed():
                        return evar[args] == ctn.splitfrac * var[args]
                    else:
                        return evar == ctn.splitfrac * var
                con = Constraint(evar.index_set(), rule=rule)
                ctn.add_component(cname, con)

        # create constraint on splitfrac vars: sum == 1
        # need to alphanum connector name in case it is indexed
        cname = unique_component_name(unit,
            "%s_frac_sum" % alphanum_label_from_name(ctr.local_name))
        con = Constraint(expr=sum(c.splitfrac for c in ctns) == 1)
        unit.add_component(cname, con)

    def Balance(unit, ctns, ctr, etype):
        # create constraint: sum of parts == the whole
        # need to alphanum connector name in case it is indexed
        for name, lst in iteritems(ctr.extensives[etype]):
            cname = unique_component_name(unit,
                "%s_%s_bal" % (alphanum_label_from_name(ctr.local_name), name))
            var = ctr.vars[name]
            def rule(m, *args):
                if len(args) == 0:
                    args = None
                return sum(evar[args] for evar in lst) == var[args]
            con = Constraint(var.index_set(), rule=rule)
            unit.add_component(cname, con)

    SplitFrac = staticmethod(SplitFrac)
    Balance = staticmethod(Balance)


class SimpleConnection(_ConnectionData, Connection):

    def __init__(self, *args, **kwds):
        _ConnectionData.__init__(self, self)
        Connection.__init__(self, *args, **kwds)

    def set_value(self, vals):
        """
        Set the connector attributes on this connection.

        If these values are being reassigned, note that the defaults
        are still None, so you may need to repass some attributes.
        """
        if not self._constructed:
            raise ValueError("Setting the value of connection '%s' before "
                             "the Connection has been constructed (there "
                             "is currently no object to set)." % self.name)
        if len(self._data) == 0:
            self._data[None] = self
        try:
            super(SimpleConnection, self).set_value(vals)
        except:
            # don't allow model walker to find poorly constructed connections
            del self._data[None]
            raise

    def pprint(self, ostream=None, verbose=False, prefix=""):
        Connection.pprint(self, ostream=ostream, verbose=verbose, prefix=prefix)


class IndexedConnection(Connection):
    def __init__(self, *args, **kwds):
        self._expanded_block = None
        super(IndexedConnection, self).__init__(*args, **kwds)

    @property
    def expanded_block(self):
        # indexed block that contains all the blocks for this connection
        return self._expanded_block


register_component(Connection, "Connection used for equating two Connectors.")


class ConnectionExpander(Plugin):
    implements(IPyomoScriptModifyInstance)

    def apply(self, **kwds):
        instance = kwds.pop('instance')
        xform = TransformationFactory('core.expand_connections')
        xform.apply_to(instance, **kwds)
        return instance

transform = ConnectionExpander()
