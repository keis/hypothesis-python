# coding=utf-8
#
# This file is part of Hypothesis, which may be found at
# https://github.com/HypothesisWorks/hypothesis-python
#
# Most of this work is copyright (C) 2013-2015 David R. MacIver
# (david@drmaciver.com), but it contains contributions by others. See
# CONTRIBUTING.rst for a full list of people who may hold copyright, and
# consult the git log if you need to determine who owns an individual
# contribution.
#
# This Source Code Form is subject to the terms of the Mozilla Public License,
# v. 2.0. If a copy of the MPL was not distributed with this file, You can
# obtain one at http://mozilla.org/MPL/2.0/.
#
# END HEADER

"""A module controlling settings for Hypothesis to use in falsification.

Either an explicit settings object can be used or the default object on
this module can be modified.

"""

from __future__ import division, print_function, absolute_import

import os
import inspect
import warnings
import threading
from enum import Enum, unique, IntEnum
from collections import namedtuple

from hypothesis.errors import InvalidArgument, HypothesisDeprecationWarning
from hypothesis.configuration import hypothesis_home_dir
from hypothesis.utils.conventions import not_set
from hypothesis.utils.dynamicvariables import DynamicVariable

__all__ = [
    'settings',
]


all_settings = {}


_db_cache = {}


class settingsProperty(object):

    def __init__(self, name):
        self.name = name

    def __get__(self, obj, type=None):
        if obj is None:
            return self
        else:
            try:
                return obj.__dict__[self.name]
            except KeyError:
                raise AttributeError(self.name)

    def __set__(self, obj, value):
        obj.__dict__[self.name] = value

    def __delete__(self, obj):
        raise AttributeError('Cannot delete attribute %s' % (self.name,))

    @property
    def __doc__(self):
        return '\n'.join((
            all_settings[self.name].description,
            'default value: %r' % (getattr(settings.default, self.name),)
        ))

default_variable = DynamicVariable(None)


class settingsMeta(type):

    def __init__(self, *args, **kwargs):
        super(settingsMeta, self).__init__(*args, **kwargs)

    @property
    def default(self):
        v = default_variable.value
        if v is not None:
            return v
        if hasattr(settings, '_current_profile'):
            settings.load_profile(settings._current_profile)
            assert default_variable.value is not None
        return default_variable.value

    @default.setter
    def default(self, value):
        raise AttributeError('Cannot assign settings.default')

    def _assign_default_internal(self, value):
        default_variable.value = value


class settings(settingsMeta('settings', (object,), {})):

    """A settings object controls a variety of parameters that are used in
    falsification. These may control both the falsification strategy and the
    details of the data that is generated.

    Default values are picked up from the settings.default object and
    changes made there will be picked up in newly created settings.

    """

    _WHITELISTED_REAL_PROPERTIES = [
        '_database', '_construction_complete', 'storage'
    ]
    __definitions_are_locked = False
    _profiles = {}

    def __getattr__(self, name):
        if name in all_settings:
            d = all_settings[name].default
            if inspect.isfunction(d):
                d = d()
            return d
        else:
            raise AttributeError('settings has no attribute %s' % (name,))

    def __init__(
            self,
            parent=None,
            **kwargs
    ):
        self._construction_complete = False
        self._database = kwargs.pop('database', not_set)
        database_file = kwargs.get('database_file', not_set)
        defaults = parent or settings.default
        if defaults is not None:
            for setting in all_settings.values():
                if kwargs.get(setting.name, not_set) is not_set:
                    kwargs[setting.name] = getattr(defaults, setting.name)
                elif setting.validator:
                    kwargs[setting.name] = setting.validator(
                        kwargs[setting.name])
            if self._database is not_set and database_file is not_set:
                self._database = defaults.database
        for name, value in kwargs.items():
            if name not in all_settings:
                raise InvalidArgument(
                    'Invalid argument %s' % (name,))
            setattr(self, name, value)
        self.storage = threading.local()
        self._construction_complete = True

    def defaults_stack(self):
        try:
            return self.storage.defaults_stack
        except AttributeError:
            self.storage.defaults_stack = []
            return self.storage.defaults_stack

    def __call__(self, test):
        test._hypothesis_internal_use_settings = self
        return test

    @classmethod
    def define_setting(
        cls, name, description, default, options=None, deprecation=None,
        validator=None,
    ):
        """Add a new setting.

        - name is the name of the property that will be used to access the
          setting. This must be a valid python identifier.
        - description will appear in the property's docstring
        - default is the default value. This may be a zero argument
          function in which case it is evaluated and its result is stored
          the first time it is accessed on any given settings object.

        """
        if settings.__definitions_are_locked:
            from hypothesis.errors import InvalidState
            raise InvalidState(
                'settings have been locked and may no longer be defined.'
            )
        if options is not None:
            options = tuple(options)
            assert default in options

        all_settings[name] = Setting(
            name, description.strip(), default, options, validator
        )
        setattr(settings, name, settingsProperty(name))

    @classmethod
    def lock_further_definitions(cls):
        settings.__definitions_are_locked = True

    def __setattr__(self, name, value):
        if name in settings._WHITELISTED_REAL_PROPERTIES:
            return object.__setattr__(self, name, value)
        elif name == 'database':
            assert self._construction_complete
            raise AttributeError(
                'settings objects are immutable and may not be assigned to'
                ' after construction.'
            )
        elif name in all_settings:
            if self._construction_complete:
                raise AttributeError(
                    'settings objects are immutable and may not be assigned to'
                    ' after construction.'
                )
            else:
                setting = all_settings[name]
                if (
                    setting.options is not None and
                    value not in setting.options
                ):
                    raise InvalidArgument(
                        'Invalid %s, %r. Valid options: %r' % (
                            name, value, setting.options
                        )
                    )
                return object.__setattr__(self, name, value)
        else:
            raise AttributeError('No such setting %s' % (name,))

    def __repr__(self):
        bits = []
        for name in all_settings:
            value = getattr(self, name)
            bits.append('%s=%r' % (name, value))
        bits.sort()
        return 'settings(%s)' % ', '.join(bits)

    @property
    def database(self):
        """An ExampleDatabase instance to use for storage of examples. May be
        None.

        If this was explicitly set at settings instantiation then that
        value will be used (even if it was None). If not and the
        database_file setting is not None this will be lazily loaded as
        an SQLite backed ExampleDatabase using that file the first time
        this property is accessed on a particular thread.

        """
        if self._database is not_set and self.database_file is not None:
            from hypothesis.database import ExampleDatabase
            if self.database_file not in _db_cache:
                _db_cache[self.database_file] = (
                    ExampleDatabase(self.database_file))
            return _db_cache[self.database_file]
        if self._database is not_set:
            self._database = None
        return self._database

    def __enter__(self):
        default_context_manager = default_variable.with_value(self)
        self.defaults_stack().append(default_context_manager)
        default_context_manager.__enter__()
        return self

    def __exit__(self, *args, **kwargs):
        default_context_manager = self.defaults_stack().pop()
        return default_context_manager.__exit__(*args, **kwargs)

    @staticmethod
    def register_profile(name, settings):
        """registers a collection of values to be used as a settings profile.
        These settings can be loaded in by name. Enable different defaults for
        different settings.

        - settings is a settings object

        """
        settings._profiles[name] = settings

    @staticmethod
    def get_profile(name):
        """Return the profile with the given name.

        - name is a string representing the name of the profile
         to load
        A InvalidArgument exception will be thrown if the
         profile does not exist

        """
        try:
            return settings._profiles[name]
        except KeyError:
            raise InvalidArgument(
                "Profile '{0}' has not been registered".format(
                    name
                )
            )

    @staticmethod
    def load_profile(name):
        """Loads in the settings defined in the profile provided If the profile
        does not exist an InvalidArgument will be thrown.

        Any setting not defined in the profile will be the library
        defined default for that setting

        """
        settings._current_profile = name
        settings._assign_default_internal(settings.get_profile(name))


Setting = namedtuple(
    'Setting', (
        'name', 'description', 'default', 'options', 'validator'
    ))


settings.define_setting(
    'min_satisfying_examples',
    default=5,
    description="""
Raise Unsatisfiable for any tests which do not produce at least this many
values that pass all assume() calls and which have not exhaustively covered the
search space.
"""
)

settings.define_setting(
    'max_examples',
    default=200,
    description="""
Once this many satisfying examples have been considered without finding any
counter-example, falsification will terminate.
"""
)

settings.define_setting(
    'max_iterations',
    default=1000,
    description="""
Once this many iterations of the example loop have run, including ones which
failed to satisfy assumptions and ones which produced duplicates, falsification
will terminate.
"""
)

settings.define_setting(
    'max_mutations',
    default=10,
    description="""
Hypothesis will try this many variations on a single example before moving on
to an entirely fresh start. If you've got hard to satisfy properties raising
this might help, but you probably shouldn't touch this dial unless you really
know what you're doing.
"""
)

settings.define_setting(
    'buffer_size',
    default=8 * 1024,
    description="""
The size of the underlying data used to generate examples. If you need to
generate really large examples you may want to increase this, but it will make
your tests slower.
"""
)


settings.define_setting(
    'max_shrinks',
    default=500,
    description="""
Once this many successful shrinks have been performed, Hypothesis will assume
something has gone a bit wrong and give up rather than continuing to try to
shrink the example.
"""
)

settings.define_setting(
    'timeout',
    default=60,
    description="""
Once this many seconds have passed, falsify will terminate even
if it has not found many examples. This is a soft rather than a hard
limit - Hypothesis won't e.g. interrupt execution of the called
function to stop it. If this value is <= 0 then no timeout will be
applied.
"""
)

settings.define_setting(
    'derandomize',
    default=False,
    description="""
If this is True then hypothesis will run in deterministic mode
where each falsification uses a random number generator that is seeded
based on the hypothesis to falsify, which will be consistent across
multiple runs. This has the advantage that it will eliminate any
randomness from your tests, which may be preferable for some situations
. It does have the disadvantage of making your tests less likely to
find novel breakages.
"""
)

settings.define_setting(
    'strict',
    default=os.getenv('HYPOTHESIS_STRICT_MODE') == 'true',
    description="""
If set to True, anything that would cause Hypothesis to issue a warning will
instead raise an error. Note that new warnings may be added at any time, so
running with strict set to True means that new Hypothesis releases may validly
break your code.

You can enable this setting temporarily by setting the HYPOTHESIS_STRICT_MODE
environment variable to the string 'true'.
"""
)

settings.define_setting(
    'database_file',
    default=lambda: (
        os.getenv('HYPOTHESIS_DATABASE_FILE') or
        os.path.join(hypothesis_home_dir(), 'examples')
    ),
    description="""
    database: An instance of hypothesis.database.ExampleDatabase that will be
used to save examples to and load previous examples from. May be None
in which case no storage will be used.
"""
)


@unique
class Phase(IntEnum):
    explicit = 0
    reuse = 1
    generate = 2
    shrink = 3


@unique
class HealthCheck(Enum):
    exception_in_generation = 0
    data_too_large = 1
    filter_too_much = 2
    too_slow = 3
    random_module = 4
    return_value = 5


class Verbosity(object):

    def __repr__(self):
        return 'Verbosity.%s' % (self.name,)

    def __init__(self, name, level):
        self.name = name
        self.level = level

    def __eq__(self, other):
        return isinstance(other, Verbosity) and (
            self.level == other.level
        )

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return self.level

    def __lt__(self, other):
        return self.level < other.level

    def __le__(self, other):
        return self.level <= other.level

    def __gt__(self, other):
        return self.level > other.level

    def __ge__(self, other):
        return self.level >= other.level

    @classmethod
    def by_name(cls, key):
        result = getattr(cls, key, None)
        if isinstance(result, Verbosity):
            return result
        raise InvalidArgument('No such verbosity level %r' % (key,))

Verbosity.quiet = Verbosity('quiet', 0)
Verbosity.normal = Verbosity('normal', 1)
Verbosity.verbose = Verbosity('verbose', 2)
Verbosity.debug = Verbosity('debug', 3)
Verbosity.all = [
    Verbosity.quiet, Verbosity.normal, Verbosity.verbose, Verbosity.debug
]


ENVIRONMENT_VERBOSITY_OVERRIDE = os.getenv('HYPOTHESIS_VERBOSITY_LEVEL')

if ENVIRONMENT_VERBOSITY_OVERRIDE:  # pragma: no cover
    DEFAULT_VERBOSITY = Verbosity.by_name(ENVIRONMENT_VERBOSITY_OVERRIDE)
else:
    DEFAULT_VERBOSITY = Verbosity.normal

settings.define_setting(
    'verbosity',
    options=Verbosity.all,
    default=DEFAULT_VERBOSITY,
    description='Control the verbosity level of Hypothesis messages',
)


def _validate_phases(phases):
    if phases is None:
        return tuple(Phase)
    phases = tuple(phases)
    for a in phases:
        if not isinstance(a, Phase):
            raise InvalidArgument('%r is not a valid phase' % (a,))
    return phases


settings.define_setting(
    'phases',
    default=tuple(Phase),
    description='Control which phases should be run',
    validator=_validate_phases,
)

settings.define_setting(
    name='stateful_step_count',
    default=50,
    description="""
Number of steps to run a stateful program for before giving up on it breaking.
"""
)

settings.define_setting(
    'perform_health_check',
    default=True,
    description=u"""
If set to True, Hypothesis will run a preliminary health check before
attempting to actually execute your test.
"""
)

settings.define_setting(
    'suppress_health_check',
    default=[],
    description="""A list of health checks to disable"""
)


settings.lock_further_definitions()

settings.register_profile('default', settings())
settings.load_profile('default')
assert settings.default is not None


def note_deprecation(message, s=None):
    # If *either* self or the current default are non-strict
    # then this should be an error. This is to handle e.g. the case
    # where defining a new setting while non-strict updates a
    # profile which is strict. This should not be an error, but
    # using the profile here would cause it to be one.
    if s is None:
        s = settings.default
    assert s is not None
    strict = settings.default.strict and s.strict
    verbosity = s.verbosity
    warning = HypothesisDeprecationWarning(message)
    if strict:
        raise warning
    elif verbosity > Verbosity.quiet:
        warnings.warn(warning, stacklevel=3)
