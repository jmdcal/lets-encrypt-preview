"""Utilities for plugins discovery and selection."""
import collections
import logging
import pkg_resources

import zope.interface

from letsencrypt import constants
from letsencrypt import errors
from letsencrypt import interfaces


class PluginEntryPoint(object):
    """Plugin entry point."""

    PREFIX_FREE_DISTRIBUTIONS = ["letsencrypt"]
    """Distributions for which prefix will be omitted."""

    # this object is mutable, don't allow it to be hashed!
    __hash__ = None

    def __init__(self, entry_point):
        self.name = self.entry_point_to_plugin_name(entry_point)
        self.plugin_cls = entry_point.load()
        self.entry_point = entry_point
        self._initialized = None
        self._prepared = None

    @classmethod
    def entry_point_to_plugin_name(cls, entry_point):
        """Unique plugin name for an ``entry_point``"""
        if entry_point.dist.key in cls.PREFIX_FREE_DISTRIBUTIONS:
            return entry_point.name
        return entry_point.dist.key + ":" + entry_point.name

    @property
    def description(self):
        """Description of the plugin."""
        return self.plugin_cls.description

    @property
    def description_with_name(self):
        """Description with name. Handy for UI."""
        return "{0} ({1})".format(self.description, self.name)

    def ifaces(self, *ifaces_groups):
        """Does plugin implements specified interface groups?"""
        return not ifaces_groups or any(
            all(iface.implementedBy(self.plugin_cls)
                for iface in ifaces)
            for ifaces in ifaces_groups)

    @property
    def initialized(self):
        """Has the plugin been initialized already?"""
        return self._initialized is not None

    def init(self, config=None):
        """Memoized plugin inititialization."""
        if not self.initialized:
            self.entry_point.require()  # fetch extras!
            self._initialized = self.plugin_cls(config, self.name)
        return self._initialized

    def verify(self, ifaces):
        """Verify that the plugin conforms to the specified interfaces."""
        assert self.initialized
        for iface in ifaces:  # zope.interface.providedBy(plugin)
            try:
                zope.interface.verify.verifyObject(iface, self.init())
            except zope.interface.exceptions.BrokenImplementation:
                if iface.implementedBy(self.plugin_cls):
                    logging.debug(
                        "%s implements %s but object does "
                        "not verify", self.plugin_cls, iface.__name__)
                return False
        return True

    @property
    def prepared(self):
        """Has the plugin been prepared already?"""
        if not self.initialized:
            logging.debug(".prepared called on uninitialized %r", self)
        return self._prepared is not None

    def prepare(self):
        """Memoized plugin preparation."""
        assert self.initialized
        if self._prepared is None:
            try:
                self._initialized.prepare()
            except errors.LetsEncryptMisconfigurationError as error:
                logging.debug("Misconfigured %r: %s", self, error)
                self._prepared = error
            except errors.LetsEncryptNoInstallationError as error:
                logging.debug("No installation (%r): %s", self, error)
                self._prepared = error
            else:
                self._prepared = True
        return self._prepared

    @property
    def misconfigured(self):
        """Is plugin misconfigured?"""
        return isinstance(
            self._prepared, errors.LetsEncryptMisconfigurationError)

    @property
    def available(self):
        """Is plugin available, i.e. prepared or misconfigured?"""
        return self._prepared is True or self.misconfigured

    def __repr__(self):
        return "PluginEntryPoint#{0}".format(self.name)

    def __str__(self):
        lines = [
            "* {0}".format(self.name),
            "Description: {0}".format(self.plugin_cls.description),
            "Interfaces: {0}".format(", ".join(
                iface.__name__ for iface in zope.interface.implementedBy(
                    self.plugin_cls))),
            "Entry point: {0}".format(self.entry_point),
        ]

        if self.initialized:
            lines.append("Initialized: {0}".format(self.init()))
            if self.prepared:
                lines.append("Prep: {0}".format(self.prepare()))

        return "\n".join(lines)


class PluginsRegistry(collections.Mapping):
    """Plugins registry."""

    def __init__(self, plugins):
        self._plugins = plugins

    @classmethod
    def find_all(cls):
        """Find plugins using setuptools entry points."""
        plugins = {}
        for entry_point in pkg_resources.iter_entry_points(
                constants.SETUPTOOLS_PLUGINS_ENTRY_POINT):
            plugin_ep = PluginEntryPoint(entry_point)
            assert plugin_ep.name not in plugins, (
                "PREFIX_FREE_DISTRIBUTIONS messed up")
            # providedBy | pylint: disable=no-member
            if interfaces.IPluginFactory.providedBy(plugin_ep.plugin_cls):
                plugins[plugin_ep.name] = plugin_ep
            else:  # pragma: no cover
                logging.warning(
                    "%r does not provide IPluginFactory, skipping", plugin_ep)
        return cls(plugins)

    def __getitem__(self, name):
        return self._plugins[name]

    def __iter__(self):
        return iter(self._plugins)

    def __len__(self):
        return len(self._plugins)

    def init(self, config):
        """Initialize all plugins in the registry."""
        return [plugin_ep.init(config) for plugin_ep
                in self._plugins.itervalues()]

    def filter(self, pred):
        """Filter plugins based on predicate."""
        return type(self)(dict((name, plugin_ep) for name, plugin_ep
                               in self._plugins.iteritems() if pred(plugin_ep)))

    def ifaces(self, *ifaces_groups):
        """Filter plugins based on interfaces."""
        # pylint: disable=star-args
        return self.filter(lambda p_ep: p_ep.ifaces(*ifaces_groups))

    def verify(self, ifaces):
        """Filter plugins based on verification."""
        return self.filter(lambda p_ep: p_ep.verify(ifaces))

    def prepare(self):
        """Prepare all plugins in the registry."""
        return [plugin_ep.prepare() for plugin_ep in self._plugins.itervalues()]

    def available(self):
        """Filter plugins based on availability."""
        return self.filter(lambda p_ep: p_ep.available)
        # succefully prepared + misconfigured

    def find_init(self, plugin):
        """Find an initialized plugin.

        This is particularly useful for finding a name for the plugin
        (although `.IPluginFactory.__call__` takes ``name`` as one of
        the arguments, ``IPlugin.name`` is not part of the interface)::

          # plugin is an instance providing IPlugin, initialized
          # somewhere else in the code
          plugin_registry.find_init(plugin).name

        Returns ``None`` if ``plugin`` is not found in the registry.

        """
        # use list instead of set beacse PluginEntryPoint is not hashable
        candidates = [plugin_ep for plugin_ep in self._plugins.itervalues()
                      if plugin_ep.initialized and plugin_ep.init() is plugin]
        assert len(candidates) <= 1
        if candidates:
            return candidates[0]
        else:
            return None

    def __repr__(self):
        return "{0}({1!r})".format(
            self.__class__.__name__, set(self._plugins.itervalues()))

    def __str__(self):
        if not self._plugins:
            return "No plugins"
        return "\n\n".join(str(p_ep) for p_ep in self._plugins.itervalues())
