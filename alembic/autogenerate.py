"""Provide the 'autogenerate' feature which can produce migration operations
automatically."""

from alembic import util
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy import schema, types as sqltypes
import re

import logging
log = logging.getLogger(__name__)

###################################################
# top level


def produce_migration_diffs(context, template_args, imports):
    opts = context.opts
    metadata = opts['target_metadata']
    if metadata is None:
        raise util.CommandError(
                "Can't proceed with --autogenerate option; environment "
                "script %s does not provide "
                "a MetaData object to the context." % (
                    context._script.env_py_location
                ))
    connection = context.bind
    diffs = []
    autogen_context = {
        'imports':imports,
        'connection':connection,
        'dialect':connection.dialect,
        'context':context,
        'opts':opts
    }
    _produce_net_changes(connection, metadata, diffs, autogen_context)
    template_args[opts['upgrade_token']] = \
            _indent(_produce_upgrade_commands(diffs, autogen_context))
    template_args[opts['downgrade_token']] = \
            _indent(_produce_downgrade_commands(diffs, autogen_context))
    template_args['imports'] = "\n".join(sorted(imports))


def _indent(text):
    text = "### commands auto generated by Alembic - please adjust! ###\n" + text
    text += "\n### end Alembic commands ###"
    text = re.compile(r'^', re.M).sub("    ", text).strip()
    return text

###################################################
# walk structures

def _produce_net_changes(connection, metadata, diffs, autogen_context):
    inspector = Inspector.from_engine(connection)
    # TODO: not hardcode alembic_version here ?
    conn_table_names = set(inspector.get_table_names()).\
                            difference(['alembic_version'])
    metadata_table_names = set(metadata.tables)

    _compare_tables(conn_table_names, metadata_table_names, inspector, metadata, diffs, autogen_context)

def _compare_tables(conn_table_names, metadata_table_names, 
                    inspector, metadata, diffs, autogen_context):
    for tname in metadata_table_names.difference(conn_table_names):
        diffs.append(("add_table", metadata.tables[tname]))
        log.info("Detected added table %r", tname)

    removal_metadata = schema.MetaData()
    for tname in conn_table_names.difference(metadata_table_names):
        exists = tname in removal_metadata.tables
        t = schema.Table(tname, removal_metadata)
        if not exists:
            inspector.reflecttable(t, None)
        diffs.append(("remove_table", t))
        log.info("Detected removed table %r", tname)

    existing_tables = conn_table_names.intersection(metadata_table_names)

    conn_column_info = dict(
        (tname, 
            dict(
                (rec["name"], rec)
                for rec in inspector.get_columns(tname)
            )
        )
        for tname in existing_tables
    )

    for tname in sorted(existing_tables):
        _compare_columns(tname, 
                conn_column_info[tname], 
                metadata.tables[tname],
                diffs, autogen_context)

    # TODO: 
    # index add/drop
    # table constraints
    # sequences

###################################################
# element comparison

def _compare_columns(tname, conn_table, metadata_table, diffs, autogen_context):
    metadata_cols_by_name = dict((c.name, c) for c in metadata_table.c)
    conn_col_names = set(conn_table)
    metadata_col_names = set(metadata_cols_by_name)

    for cname in metadata_col_names.difference(conn_col_names):
        diffs.append(
            ("add_column", tname, metadata_cols_by_name[cname])
        )
        log.info("Detected added column '%s.%s'", tname, cname)

    for cname in conn_col_names.difference(metadata_col_names):
        diffs.append(
            ("remove_column", tname, schema.Column(
                cname,
                conn_table[cname]['type'],
                nullable=conn_table[cname]['nullable'],
                server_default=conn_table[cname]['default']
            ))
        )
        log.info("Detected removed column '%s.%s'", tname, cname)

    for colname in metadata_col_names.intersection(conn_col_names):
        metadata_col = metadata_table.c[colname]
        conn_col = conn_table[colname]
        col_diff = []
        _compare_type(tname, colname,
            conn_col,
            metadata_col,
            col_diff, autogen_context
        )
        _compare_nullable(tname, colname,
            conn_col,
            metadata_col.nullable,
            col_diff, autogen_context
        )
        _compare_server_default(tname, colname,
            conn_col,
            metadata_col,
            col_diff, autogen_context
        )
        if col_diff:
            diffs.append(col_diff)

def _compare_nullable(tname, cname, conn_col, 
                            metadata_col_nullable, diffs, 
                            autogen_context):
    conn_col_nullable = conn_col['nullable']
    if conn_col_nullable is not metadata_col_nullable:
        diffs.append(
            ("modify_nullable", tname, cname, 
                {
                    "existing_type":conn_col['type'],
                    "existing_server_default":conn_col['default'],
                },
                conn_col_nullable, 
                metadata_col_nullable),
        )
        log.info("Detected %s on column '%s.%s'", 
            "NULL" if metadata_col_nullable else "NOT NULL",
            tname,
            cname
        )

def _compare_type(tname, cname, conn_col, 
                            metadata_col, diffs, 
                            autogen_context):

    conn_type = conn_col['type']
    metadata_type = metadata_col.type
    if conn_type._type_affinity is sqltypes.NullType:
        log.info("Couldn't determine database type for column '%s.%s'" % (tname, cname))
        return
    if metadata_type._type_affinity is sqltypes.NullType:
        log.info("Column '%s.%s' has no type within the model; can't compare" % (tname, cname))
        return

    isdiff = autogen_context['context'].compare_type(conn_col, metadata_col)

    if isdiff:

        diffs.append(
            ("modify_type", tname, cname, 
                    {
                        "existing_nullable":conn_col['nullable'],
                        "existing_server_default":conn_col['default'],
                    },
                    conn_type, 
                    metadata_type),
        )
        log.info("Detected type change from %r to %r on '%s.%s'", 
            conn_type, metadata_type, tname, cname
        )

def _compare_server_default(tname, cname, conn_col, metadata_col, 
                                diffs, autogen_context):

    metadata_default = metadata_col.server_default
    conn_col_default = conn_col['default']
    if conn_col_default is None and metadata_default is None:
        return False
    rendered_metadata_default = _render_server_default(metadata_default, autogen_context)
    isdiff = autogen_context['context'].compare_server_default(
                        conn_col, metadata_col,
                        rendered_metadata_default
                    )
    if isdiff:
        conn_col_default = conn_col['default']
        diffs.append(
            ("modify_default", tname, cname, 
                {
                    "existing_nullable":conn_col['nullable'],
                    "existing_type":conn_col['type'],
                },
                conn_col_default,
                metadata_default),
        )
        log.info("Detected server default on column '%s.%s'", 
            tname,
            cname
        )


###################################################
# produce command structure

def _produce_upgrade_commands(diffs, autogen_context):
    buf = []
    for diff in diffs:
        buf.append(_invoke_command("upgrade", diff, autogen_context))
    if not buf:
        buf = ["pass"]
    return "\n".join(buf)

def _produce_downgrade_commands(diffs, autogen_context):
    buf = []
    for diff in diffs:
        buf.append(_invoke_command("downgrade", diff, autogen_context))
    if not buf:
        buf = ["pass"]
    return "\n".join(buf)

def _invoke_command(updown, args, autogen_context):
    if isinstance(args, tuple):
        return _invoke_adddrop_command(updown, args, autogen_context)
    else:
        return _invoke_modify_command(updown, args, autogen_context)

def _invoke_adddrop_command(updown, args, autogen_context):
    cmd_type = args[0]
    adddrop, cmd_type = cmd_type.split("_")

    cmd_args = args[1:] + (autogen_context,)

    _commands = {
        "table":(_drop_table, _add_table),
        "column":(_drop_column, _add_column),
    }

    cmd_callables = _commands[cmd_type]

    if (
        updown == "upgrade" and adddrop == "add"
    ) or (
        updown == "downgrade" and adddrop == "remove"
    ):
        return cmd_callables[1](*cmd_args)
    else:
        return cmd_callables[0](*cmd_args)

def _invoke_modify_command(updown, args, autogen_context):
    tname, cname = args[0][1:3]
    kw = {}

    _arg_struct = {
        "modify_type":("existing_type", "type_"),
        "modify_nullable":("existing_nullable", "nullable"),
        "modify_default":("existing_server_default", "server_default"),
    }
    for diff in args:
        diff_kw = diff[3]
        for arg in ("existing_type", \
                "existing_nullable", \
                "existing_server_default"):
            if arg in diff_kw:
                kw.setdefault(arg, diff_kw[arg])
        old_kw, new_kw = _arg_struct[diff[0]]
        if updown == "upgrade":
            kw[new_kw] = diff[-1]
            kw[old_kw] = diff[-2]
        else:
            kw[new_kw] = diff[-2]
            kw[old_kw] = diff[-1]

    if "nullable" in kw:
        kw.pop("existing_nullable", None)
    if "server_default" in kw:
        kw.pop("existing_server_default", None)
    return _modify_col(tname, cname, autogen_context, **kw)

###################################################
# render python

def _add_table(table, autogen_context):
    return "%(prefix)screate_table(%(tablename)r,\n%(args)s\n)" % {
        'tablename':table.name,
        'prefix':_alembic_autogenerate_prefix(autogen_context),
        'args':',\n'.join(
            [_render_column(col, autogen_context) for col in table.c] +
            sorted([rcons for rcons in 
                [_render_constraint(cons, autogen_context) for cons in 
                    table.constraints]
                if rcons is not None
            ])
        ),
    }

def _drop_table(table, autogen_context):
    return "%(prefix)sdrop_table(%(tname)r)" % {
            "prefix":_alembic_autogenerate_prefix(autogen_context),
            "tname":table.name
        }

def _add_column(tname, column, autogen_context):
    return "%(prefix)sadd_column(%(tname)r, %(column)s)" % {
            "prefix":_alembic_autogenerate_prefix(autogen_context),
            "tname":tname,
            "column":_render_column(column, autogen_context)
            }

def _drop_column(tname, column, autogen_context):
    return "%(prefix)sdrop_column(%(tname)r, %(cname)r)" % {
            "prefix":_alembic_autogenerate_prefix(autogen_context),
            "tname":tname,
            "cname":column.name
            }

def _modify_col(tname, cname, 
                autogen_context,
                server_default=False,
                type_=None,
                nullable=None,
                existing_type=None,
                existing_nullable=None,
                existing_server_default=False):
    sqla_prefix = _sqlalchemy_autogenerate_prefix(autogen_context)
    indent = " " * 11
    text = "%(prefix)salter_column(%(tname)r, %(cname)r" % {
                            'prefix':_alembic_autogenerate_prefix(autogen_context), 
                            'tname':tname, 
                            'cname':cname}
    text += ", \n%sexisting_type=%s" % (indent, 
                    _repr_type(sqla_prefix, existing_type, autogen_context))
    if server_default is not False:
        text += ", \n%sserver_default=%s" % (indent, 
                        _render_server_default(server_default, autogen_context),)
    if type_ is not None:
        text += ", \n%stype_=%s" % (indent, _repr_type(sqla_prefix, type_, autogen_context))
    if nullable is not None:
        text += ", \n%snullable=%r" % (
                        indent, nullable,)
    if existing_nullable is not None:
        text += ", \n%sexisting_nullable=%r" % (
                        indent, existing_nullable)
    if existing_server_default:
        text += ", \n%sexisting_server_default=%s" % (
                        indent, 
                        _render_server_default(
                            existing_server_default, 
                            autogen_context),
                    )
    text += ")"
    return text

def _sqlalchemy_autogenerate_prefix(autogen_context):
    return autogen_context['opts']['sqlalchemy_module_prefix'] or ''

def _alembic_autogenerate_prefix(autogen_context):
    return autogen_context['opts']['alembic_module_prefix'] or ''

def _render_column(column, autogen_context):
    opts = []
    if column.server_default:
        opts.append(("server_default", 
                    _render_server_default(column.server_default, autogen_context)))
    if column.nullable is not None:
        opts.append(("nullable", column.nullable))

    # TODO: for non-ascii colname, assign a "key"
    return "%(prefix)sColumn(%(name)r, %(type)s, %(kw)s)" % {
        'prefix':_sqlalchemy_autogenerate_prefix(autogen_context),
        'name':column.name,
        'type':_repr_type(_sqlalchemy_autogenerate_prefix(autogen_context), column.type, autogen_context),
        'kw':", ".join(["%s=%s" % (kwname, val) for kwname, val in opts])
    }

def _render_server_default(default, autogen_context):
    if isinstance(default, schema.DefaultClause):
        if isinstance(default.arg, basestring):
            default = default.arg
        else:
            default = str(default.arg.compile(dialect=autogen_context['dialect']))
    if isinstance(default, basestring):
        # TODO: this is just a hack to get 
        # tests to pass until we figure out
        # WTF sqlite is doing
        default = re.sub(r"^'|'$", "", default)
        return "'%s'" % default
    else:
        return None

def _repr_type(prefix, type_, autogen_context):
    mod = type(type_).__module__
    imports = autogen_context.get('imports', None)
    if mod.startswith("sqlalchemy.dialects"):
        dname = re.match(r"sqlalchemy\.dialects\.(\w+)", mod).group(1)
        if imports is not None:
            imports.add("from sqlalchemy.dialects import %s" % dname)
        return "%s.%r" % (dname, type_)
    else:
        return "%s%r" % (prefix, type_)

def _render_constraint(constraint, autogen_context):
    renderer = _constraint_renderers.get(type(constraint), None)
    if renderer:
        return renderer(constraint, autogen_context)
    else:
        return None

def _render_primary_key(constraint, autogen_context):
    opts = []
    if constraint.name:
        opts.append(("name", repr(constraint.name)))
    return "%(prefix)sPrimaryKeyConstraint(%(args)s)" % {
        "prefix":_sqlalchemy_autogenerate_prefix(autogen_context),
        "args":", ".join(
            [repr(c.key) for c in constraint.columns] +
            ["%s=%s" % (kwname, val) for kwname, val in opts]
        ),
    }

def _render_foreign_key(constraint, autogen_context):
    opts = []
    if constraint.name:
        opts.append(("name", repr(constraint.name)))
    # TODO: deferrable, initially, etc.
    return "%(prefix)sForeignKeyConstraint([%(cols)s], [%(refcols)s], %(args)s)" % {
        "prefix":_sqlalchemy_autogenerate_prefix(autogen_context),
        "cols":", ".join("'%s'" % f.parent.key for f in constraint.elements),
        "refcols":", ".join(repr(f._get_colspec()) for f in constraint.elements),
        "args":", ".join(
            ["%s=%s" % (kwname, val) for kwname, val in opts]
        ),
    }

def _render_check_constraint(constraint, autogen_context):
    opts = []
    if constraint.name:
        opts.append(("name", repr(constraint.name)))
    return "%(prefix)sCheckConstraint('TODO')" % {
            "prefix":_sqlalchemy_autogenerate_prefix(autogen_context)
        }

_constraint_renderers = {
    schema.PrimaryKeyConstraint:_render_primary_key,
    schema.ForeignKeyConstraint:_render_foreign_key,
    schema.CheckConstraint:_render_check_constraint
}
