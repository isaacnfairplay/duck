from __future__ import annotations
from functools import wraps
import time
import inspect
import os
import re
import uuid
import csv
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Type,
    TypeVar,
    Union,
)

from decimal import Decimal
import duckdb

# ===== ODBC / nanodbc integration helpers & defaults =====
import getpass
import keyring

# nanodbc read defaults (kept separate from CSV/Parquet defaults)
DEFAULT_ODBC_ENCODING = "UTF-8"
DEFAULT_ODBC_ALL_VARCHAR = False
DEFAULT_ODBC_READ_ONLY = True

def _ensure_nanoodbc_loaded(con: duckdb.DuckDBPyConnection) -> None:
    """Ensure the DuckDB nanodbc extension is available (LOAD, else INSTALL+LOAD)."""
    try:
        con.execute("LOAD nanodbc;")
        return
    except Exception:
        print("nanodbc not loaded; attempting INSTALL nanodbc FROM community...")
    try:
        con.execute("INSTALL nanodbc FROM community;")
        con.execute("LOAD nanodbc;")
    except Exception as e_second:
        
        print(type(e_second))
        input(str(e_second))
        raise RuntimeError(
            "Failed to LOAD (and INSTALL) DuckDB nanodbc extension. "
            "Run `INSTALL nanodbc FROM community; LOAD nanodbc;` inside DuckDB, "
            "or ensure the extension is available."
        ) from e_second

def _odbc_brace_value(val: str) -> str:
    """Value={...} wrapper (escapes '}') so ODBC can safely contain ';' and spaces."""
    needs = any(ch in val for ch in ";{}") or val.strip() != val
    if needs:
        return "{" + val.replace("}", "}}") + "}"
    return val

def _odbc_kv(k: str, v: str | None) -> str:
    return f"{k}={_odbc_brace_value(v or '')}"

def _odbc_verify(con: duckdb.DuckDBPyConnection, odbc_conn_str: str) -> bool:
    """Lightweight connection verification via SELECT 1."""
    _ensure_nanoodbc_loaded(con)
    lower_con = odbc_conn_str.lower()
    try:
        con.sql(
            "SELECT * FROM odbc_query("
            f"connection={_sql_literal(odbc_conn_str)}, "
            f"query={_sql_literal('SELECT 1')}, "
            f"all_varchar={str(DEFAULT_ODBC_ALL_VARCHAR).lower()}, "
            f"encoding={_sql_literal(DEFAULT_ODBC_ENCODING)}, "
            f"read_only={str(DEFAULT_ODBC_READ_ONLY).lower()}"
            ")"
        )
        return True
    except duckdb.BinderException as e:
        return True
    except Exception as e:
        print(type(e))
        print(e)
        input("Did you expect this?")
        return False

def _build_conn_str_dsn(
    *,
    dsn: str,
    use_trusted_connection: bool,
    username: str | None = None,
    password: str | None = None,
    extra: Mapping[str,str] | None = None,
) -> str:
    parts = [_odbc_kv("DSN", dsn)]
    if use_trusted_connection:
        parts.append(_odbc_kv("Trusted_Connection", "Yes"))
    else:
        if not username or not password:
            raise ValueError("Username and password required when not using Trusted_Connection.")
        parts.append(_odbc_kv("UID", username))
        parts.append(_odbc_kv("PWD", password))
    if extra:
        parts.extend(_odbc_kv(k, v) for k, v in extra.items())
    return ";".join(parts) + ";"

def _build_conn_str_sqlserver(
    *,
    server: str,
    database: str,
    driver: Literal[
        "ODBC Driver 18 for SQL Server",
        "ODBC Driver 17 for SQL Server",
        "SQL Server",
        "SQL Server Native Client 11.0",
    ],
    use_trusted_connection: bool,
    username: str | None = None,
    password: str | None = None,
    encrypt: str | None = None,
    trust_server_certificate: str | None = None,
    extra: Mapping[str,str] | None = None,
) -> str:
    parts = [
        _odbc_kv("DRIVER", driver),
        _odbc_kv("SERVER", server),
        _odbc_kv("DATABASE", database),
    ]
    if use_trusted_connection:
        parts.append(_odbc_kv("Trusted_Connection", "Yes"))  # 'Yes' not TRUE
    else:
        if not username or not password:
            raise ValueError("Username and password required when not using Trusted_Connection.")
        parts.append(_odbc_kv("UID", username))
        parts.append(_odbc_kv("PWD", password))
    if encrypt is not None:
        parts.append(_odbc_kv("Encrypt", encrypt))
    if trust_server_certificate is not None:
        parts.append(_odbc_kv("TrustServerCertificate", trust_server_certificate))
    if extra:
        parts.extend(_odbc_kv(k, v) for k, v in extra.items())
    return ";".join(parts) + ";"

def _build_conn_str_excel(
    *,
    path: str,
    driver: Literal["Microsoft Excel Driver (*.xls, *.xlsx, *.xlsm, *.xlsb)"] = "Microsoft Excel Driver (*.xls, *.xlsx, *.xlsm, *.xlsb)",
    read_only: bool = True,
    extended_properties: str | None = None,
    extra: Mapping[str,str] | None = None,
) -> str:
    if extended_properties is None:
        ext = 'Excel 8.0;HDR=Yes;IMEX=1' if path.lower().endswith(".xls") else 'Excel 12.0 Xml;HDR=Yes;IMEX=1'
    else:
        ext = extended_properties
    parts = [
        _odbc_kv("Driver", driver),
        _odbc_kv("DBQ", path),
        _odbc_kv("ReadOnly", "1" if read_only else "0"),
        _odbc_kv("Extended Properties", ext),
    ]
    if extra:
        parts.extend(_odbc_kv(k, v) for k, v in extra.items())
    return ";".join(parts) + ";"

def _get_or_ask_password(
    *,
    service: str,
    username: str,
    con: duckdb.DuckDBPyConnection | None,
    precheck: bool,
    build_conn_for_verify: Callable[[str,str], str] | None,
) -> str:
    """OS keyring-backed password: read-or-prompt, optional verify via nanodbc."""
    password = keyring.get_password(service, username)

    if precheck and password is not None:
        if con is None or build_conn_for_verify is None:
            raise ValueError("precheck=True requires con and build_conn_for_verify.")
        if _odbc_verify(con, build_conn_for_verify(username, password)):
            return password
        # rotate label
        old_key = f"old_{username}"
        print(f"Saved password invalid for {service}@{username}; rotating to {old_key}")
        keyring.set_password(service, old_key, password)
        try:
            keyring.delete_password(service, username)
        except Exception:
            pass
        password = None

    while password is None:
        print(f"Password required for {service}@{username}.")
        pw1 = getpass.getpass("Enter password: ")
        pw2 = getpass.getpass("Confirm password: ")
        if pw1 != pw2:
            print("Passwords did not match; try again.\n")
            continue
        keyring.set_password(service, username, pw1)
        password = keyring.get_password(service, username)

        if precheck:
            if con is None or build_conn_for_verify is None:
                raise ValueError("precheck=True requires con and build_conn_for_verify.")
            assert password is not None
            if _odbc_verify(con, build_conn_for_verify(username, password)):
                print(f"Password set and verified for {service}@{username}")
                return password
            print("The provided password didn't work; please try again.\n")
            try:
                keyring.delete_password(service, username)
            except Exception:
                pass
            password = None
        else:
            print(f"Password set for {service}@{username}")
            assert password is not None
            return password

    assert password is not None
    return password


_np = None
try:
    import numpy as _np
except ImportError:
    pass
    


# =========================
# Public type aliases (3.13 style) for I/O options
# =========================

# --- Encodings (extend as you need; lower-case string literals for ergonomics)
Encoding = Literal[
    'utf-8', 'utf8', 'utf-16', 'utf-16le', 'utf-16be',
    'latin-1', 'iso-8859-1', 'cp1252', 'cp932'
]

# --- CSV quoting as ergonomic lowercase strings mapped to csv constants
CsvQuoting = Literal['minimal', 'all', 'none', 'nonnumeric']
_QUOTING_MAP: dict[CsvQuoting, int] = {
    'minimal': csv.QUOTE_MINIMAL,
    'all': csv.QUOTE_ALL,
    'none': csv.QUOTE_NONE,
    'nonnumeric': csv.QUOTE_NONNUMERIC,
}

# --- CSV compression names (DuckDB supports auto, gzip, bz2, zstd)
CsvCompression = Literal['auto', 'gzip', 'bz2', 'zstd']

# --- Parquet compression (DuckDB docs)
ParquetCompression = Literal[
    'uncompressed', 'snappy', 'gzip', 'zstd', 'brotli', 'lz4', 'lz4_raw'
]

# --- JSON format/records modes (kept loose per docs but constrained where useful)
JsonFormat = Literal['auto', 'newline_delimited', 'json', 'jsonl']
JsonRecords = Literal['auto', 'line', 'array']


# =========================
# Errors
# =========================

class DuckError(Exception):
    """Base class for all Duck wrapper errors."""


class DuckTableTargetError(DuckError):
    """Raised when an operation requires table-targeting mode (and we don't have it)."""


class DuckIOError(DuckError):
    """Errors reading/writing external data."""


class DuckTypeError(DuckError):
    """Identifier/type validation errors."""


class DuckJoinPartitionError(DuckError):
    """Errors related to partitioned join orchestration."""


class DuckStateError(DuckError):
    """Illegal internal state or mode transitions."""


# =========================
# Typing
# =========================

T = TypeVar('T')
_SIMPLE_IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


# =========================
# Identifier helpers (public + internal)
# =========================

def validate_identifiers(*names: str) -> None:
    """Public helper — raises DuckTypeError on invalid identifiers."""
    for n in names:
        ensure_no_quotes(n)
        ensure_simple_ident(n)


def ensure_no_quotes(name: str) -> None:
    if '"' in name:
        raise DuckTypeError(f'Identifier contains a double quote ("): {name!r}')


def ensure_simple_ident(name: str) -> None:
    if not _SIMPLE_IDENT.match(name):
        raise DuckTypeError(f'Identifier must be [A-Za-z_][A-Za-z0-9_]*: {name!r}')


def case_fold_map(names: Iterable[str]) -> Dict[str, str]:
    return {n.casefold(): n for n in names}


def resolve_existing(
        columns: Sequence[str],
        requested: Iterable[str],
        skip_missing: bool,
) -> Tuple[List[str], List[str]]:
    existing = case_fold_map(columns)
    resolved = [existing[r.casefold()] for r in requested if r.casefold() in existing]
    missing = [r for r in requested if r.casefold() not in existing]
    if not skip_missing and missing:
        raise KeyError('Missing columns: ' + ', '.join(sorted(missing)))
    return resolved, missing


# =========================
# Small runtime helpers
# =========================

def _as_path_str(p: str | Path) -> str:
    return str(p) if isinstance(p, Path) else p


def _fetch_column(rel: duckdb.DuckDBPyRelation, column: int = 0) -> List[Any]:
    cols = rel.columns
    if not cols:
        return []
    if column < 0 or column >= len(cols):
        raise IndexError(f'Column {column} out of range')
    only = rel.select(cols[column])
    return [r[0] for r in only.fetchall()]


def _coerce_and_check(value: Any, expected_type: type | tuple[type, ...], allow_none: bool) -> Any:
    if value is None:
        if allow_none:
            return None
        raise TypeError('Got None but allow_none=False.')
    if not isinstance(value, expected_type):
        # numeric coercions (int/float/Decimal)
        if expected_type == int and isinstance(value, (float, Decimal)) and float(value).is_integer():
            return int(float(value))
        if expected_type == float and isinstance(value, (int, Decimal)):
            return float(value)
        raise TypeError(f'Expected {expected_type}, got {type(value)}: {value!r}')
    return value


def _nonnull_value_error(value: Any) -> Any:
    if value is None:
        raise ValueError('Encountered NULL where a non-NULL value was required.')
    return value


def _empty_for_expected_type(expected_type: type | tuple[type, ...]) -> Optional[Any]:
    if isinstance(expected_type, tuple):
        tset = set(expected_type)
        if list in tset:
            return []
        if dict in tset:
            return {}
        if tuple in tset:
            return ()
        if set in tset:
            return set()
        return None
    return [] if expected_type is list else {} if expected_type is dict else \
        () if expected_type is tuple else set() if expected_type is set else None


def _hex_to_rgb(s: str) -> Tuple[int, int, int]:
    s = s.strip().lstrip('#')
    if len(s) == 3:
        s = ''.join(ch * 2 for ch in s)
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def _rgb_to_hex(rgb: Tuple[int, int, int]) -> str:
    r, g, b = rgb
    return f'#{r:02x}{g:02x}{b:02x}'


def _linear_interpolate_color_color(c0: str, c1: str, t: float) -> str:
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    r0, g0, b0 = _hex_to_rgb(c0)
    r1, g1, b1 = _hex_to_rgb(c1)
    return _rgb_to_hex((int(r0 + (r1 - r0) * t), int(g0 + (g1 - g0) * t), int(b0 + (b1 - b0) * t)))


def _typed_cell(
        rel: duckdb.DuckDBPyRelation,
        typ: Type[T] | Tuple[Type[T], ...],
        *,
        row: int = 0,
        col: int = 0,
        allow_none: bool = True,
) -> Optional[T]:
    """
    Fetch a single cell (row, col). Returns Optional[T] if allow_none=True, else T.

    NOTE: If you pass parameterized generics like list[int], we only validate the outer
    container at runtime (mypy cannot enforce parametric types from values).
    """
    rows = rel.limit(row + 1).fetchall()
    if not rows or len(rows) <= row:
        return None if allow_none else _nonnull_value_error(None)
    if not rows[row] or len(rows[row]) <= col:
        return None if allow_none else _nonnull_value_error(None)

    v = rows[row][col]
    if v is None:
        return None if allow_none else _nonnull_value_error(v)

    if isinstance(typ, tuple):
        if any(isinstance(v, t) for t in typ):
            return v  # type: ignore[return-value]
        if int in typ and isinstance(v, (float, Decimal)) and float(v).is_integer():
            return int(float(v))  # type: ignore[return-value]
        if float in typ and isinstance(v, (int, Decimal)):
            return float(v)  # type: ignore[return-value]
        raise TypeError(f'Expected {typ}, got {type(v)}: {v!r}')

    coerced = _coerce_and_check(v, typ, allow_none=False)
    return coerced  # type: ignore[return-value]


def _sql_literal(value: Any) -> str:
    """Render a Python value as a DuckDB SQL literal."""
    if value is None:
        return 'NULL'
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, (list, tuple)):
        return '[' + ', '.join(_sql_literal(v) for v in value) + ']'
    if isinstance(value, Mapping):
        items: List[str] = []
        for k, v in value.items():
            k_str = str(k)
            key = k_str if _SIMPLE_IDENT.match(k_str) else '"' + k_str.replace('"', '""') + '"'
            items.append(f"{key}: {_sql_literal(v)}")
        return '{' + ', '.join(items) + '}'
    return _sql_literal(str(value))


# =========================
# Filesystem & partition matching
# =========================

def _iter_files_under(root: Path) -> Iterable[Path]:
    """Efficient recursive dir walk yielding files only (uses os.scandir)."""
    stack = [root]
    while stack:
        p = stack.pop()
        try:
            with os.scandir(p) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(Path(e.path))
                    elif e.is_file(follow_symlinks=False):
                        yield Path(e.path)
        except FileNotFoundError:
            continue


def _path_partition_vector(fp: Path, depth: int) -> List[str]:
    """
    Partition vector (leaf to root):
      index 0 -> file stem  | 'stem'
      index 1 -> parent     | 'parent'
      index 2 -> parent2    | 'parent2'
      index 3 -> parent3    | 'parent3'
      ...
    """
    out: List[str] = [fp.stem]
    cur = fp.parent
    while len(out) < depth and cur.name:
        out.append(cur.name)
        cur = cur.parent
    return out


def _match_one_partition(comp: str, flt: Union[str, int]) -> bool:
    return comp == str(flt)


def _vector_matches_filters(
        vec: Sequence[str],
        filters: Sequence[Optional[Union[str, int, Sequence[Union[str, int]]]]],
        *,
        hive_style: bool,  # literal compare either way; keep param for API symmetry
) -> bool:
    if hive_style:
        pass
    for i, f in enumerate(filters):
        if f is None:
            continue
        comp = vec[i] if i < len(vec) else ''
        if isinstance(f, (list, tuple, set)):
            if not any(_match_one_partition(comp, v) for v in f):
                return False
        else:
            if not _match_one_partition(comp, f):
                return False
    return True

def key_to_index(k: str) -> int:
        k_cf = k.casefold()
        if k_cf == 'stem':
            return 0
        if k_cf == 'parent':
            return 1
        if k_cf.startswith('parent'):
            suffix = k_cf[len('parent'):]
            if not suffix.isdigit():
                raise DuckTypeError(f"Invalid partition key '{k}'; expected 'parentN' with N>=2.")
            n = int(suffix)
            if n < 2:
                raise DuckTypeError(f"Invalid partition key '{k}'; use 'parent' for index 1.")
            return n
        raise DuckTypeError(f"Unrecognized partition key '{k}'. Use 'stem', 'parent', 'parent2', ...")


def _normalize_partition_filters(
        pf: Optional[
            Union[
                Sequence[Optional[Union[str, int, Sequence[Union[str, int]]]]],
                Mapping[str, Optional[Union[str, int, Sequence[Union[str, int]]]]],
            ]
        ]
) -> Optional[List[Optional[Union[str, int, Sequence[Union[str, int]]]]]]:
    """
    Accept either:
      1) Positional sequence aligned to [stem, parent, parent2, ...]
      2) Mapping with named keys: {'stem': ... , 'parent': ..., 'parent2': ...}
    """
    if pf is None:
        return None
    if isinstance(pf, (list, tuple)):
        return list(pf)
    if not isinstance(pf, Mapping):
        raise DuckTypeError('partition_filters must be a Sequence or a Mapping.')

    index_map: Dict[int, Optional[Union[str, int, Sequence[Union[str, int]]]]] = {}
    max_idx = 0
    for k, v in pf.items():
        idx = key_to_index(str(k))
        index_map[idx] = v
        if idx > max_idx:
            max_idx = idx

    out: List[Optional[Union[str, int, Sequence[Union[str, int]]]]] = [None] * (max_idx + 1)
    for idx, val in index_map.items():
        out[idx] = val
    return out


def _collect_paths(
        sources: Union[str, Path, Sequence[Union[str, Path]]],
        extensions: Tuple[str, ...],
        *,
        partition_filters: Optional[
            Union[
                Sequence[Optional[Union[str, int, Sequence[Union[str, int]]]]],
                Mapping[str, Optional[Union[str, int, Sequence[Union[str, int]]]]],
            ]
        ] = None,
        hive_style: bool = False,
) -> List[str]:
    """Normalize 'sources' into a list of matching file paths."""
    if isinstance(sources, (str, Path)):
        src_list: List[Path] = [Path(sources)]
    else:
        src_list = [Path(s) for s in sources]

    filters_pos = _normalize_partition_filters(partition_filters)
    depth = len(filters_pos) if filters_pos is not None else 0

    out: List[str] = []
    for src in src_list:
        if src.is_file():
            if src.suffix.lower() in extensions:
                if filters_pos:
                    vec = _path_partition_vector(src, depth)
                    if _vector_matches_filters(vec, filters_pos, hive_style=hive_style):
                        out.append(str(src))
                else:
                    out.append(str(src))
        elif src.is_dir():
            for fp in _iter_files_under(src):
                if fp.suffix.lower() in extensions:
                    if filters_pos:
                        vec = _path_partition_vector(fp, depth)
                        if _vector_matches_filters(vec, filters_pos, hive_style=hive_style):
                            out.append(str(fp))
                    else:
                        out.append(str(fp))
    return out


def safe_filename(value: Any) -> str:
    """Sanitize a partition value into a filesystem-friendly component."""
    s = str(value).strip()
    s = re.sub(r'[\\/:*?"<>|]+', '_', s)
    s = re.sub(r'\s+', '_', s)
    return s or 'part'


# =========================
# Connection.register helpers (no temp views)
# =========================

def _reg_name(prefix: str) -> str:
    return f'{prefix}_{uuid.uuid4().hex}'


def _reg1(con: duckdb.DuckDBPyConnection, rel: duckdb.DuckDBPyRelation) -> str:
    name = _reg_name('r')
    con.register(name, rel)
    return name


def _reg2(con: duckdb.DuckDBPyConnection, rel_a: duckdb.DuckDBPyRelation, rel_b: duckdb.DuckDBPyRelation) -> Tuple[str, str]:
    a = _reg_name('a')
    b = _reg_name('b')
    con.register(a, rel_a)
    con.register(b, rel_b)
    return a, b


# =========================
# Duck wrapper
# =========================

class Duck:
    """
    Wrapper around DuckDB relations with Pythonic helpers, strong typing, and
    typed/ergonomic I/O options.

    Key design choices:
      - Public I/O APIs use **lowercase string literals** (e.g., 'gzip', 'utf-8', 'minimal')
        for discoverability and IDE help, then translate to DuckDB’s expected values.
      - We do not blindly **kwargs into DuckDB; every option is explicit and typed**.
      - All reader helpers support list/dir/glob via our own path collector with optional
        partition filters (by path segments).
    """

    def __init__(
            self,
            con: duckdb.DuckDBPyConnection,
            rel: duckdb.DuckDBPyRelation,
            *,
            table_name: Optional[str] = None,
    ) -> None:
        self.con = con
        self.rel = rel
        self._table_name: Optional[str] = table_name  # table-targeting flag

    # ---------- basics ----------
    @property
    def columns(self) -> tuple[str, ...]:
        return tuple([v.lower() for v in self.rel.columns])

    def __str__(self) -> str:  # pragma: no cover
        return str(self.rel)

    def __repr__(self) -> str:  # pragma: no cover
        return f'Duck(rel={repr(self.rel)}, table={self._table_name!r})'

    def show(self) -> None:
        """Pretty-print first n rows using DuckDB's display."""
        self.rel.show()  

    # ---------- scalar getters ----------
    def cell_value(
            self,
            *,
            expected_type: Type[T] | Tuple[Type[T], ...] = object,
            row: int = 0,
            col: int = 0,
    ) -> Optional[T]:
        """
        Return Optional[T]; None if empty/NULL. If expected_type is object, no runtime type check.
        """
        if expected_type is object:
            rows = self.rel.limit(row + 1).fetchall()
            if not rows or len(rows) <= row:
                return None
            if not rows[row] or len(rows[row]) <= col:
                return None
            return rows[row][col]  # Optional[Any]
        return _typed_cell(self.rel, expected_type, row=row, col=col, allow_none=True)

    def cell_value_notnull(
            self,
            *,
            expected_type: Type[T] | Tuple[Type[T], ...],
            row: int = 0,
            col: int = 0,
    ) -> T:
        """Return T; raises ValueError if empty/NULL."""
        val = _typed_cell(self.rel, expected_type, row=row, col=col, allow_none=False)
        return val  # type: ignore[return-value]

    def to_dict(self) -> Dict[str, Any]:
        """First row as {col: value}; empty -> {}."""
        cols = self.columns
        rows = self.rel.limit(1).fetchall()
        if not rows:
            return {}
        r = rows[0]
        return {cols[i]: r[i] for i in range(len(cols))}

    # ---------- select-style transforms ----------
    def _clear_table_target(self) -> None:
        self._table_name = None

    def rename(self, *, skip_missing: bool = False, **new_to_old: str) -> 'Duck':
        """
        Rename columns by passing new_name=old_name. If skip_missing=True,
        missing sources are ignored (no-op).
        """
        if not new_to_old:
            return self
        for new_name, old_name in new_to_old.items():
            validate_identifiers(new_name)
            ensure_no_quotes(old_name)
        requested_old = list(new_to_old.values())
        resolved_old, missing = resolve_existing(self.columns, requested_old, skip_missing)
        if missing and not skip_missing:
            raise KeyError('Missing columns: ' + ', '.join(missing))
        if not resolved_old and skip_missing:
            return self
        missing_set = {m.casefold() for m in missing}
        pairs = []
        for new_name, old_name in new_to_old.items():
            if skip_missing and old_name.casefold() in missing_set:
                continue
            actual_old = next((a for a in resolved_old if a.casefold() == old_name.casefold()), None)
            if actual_old is not None:
                pairs.append((actual_old, new_name))
        if not pairs:
            return self
        clause = ', '.join(f'"{old}" AS "{new}"' for (old, new) in pairs)
        out = Duck(self.con, self.rel.select(f'* RENAME ({clause})'))
        out._clear_table_target()
        return out

    def exclude(self, *cols: str, skip_missing: bool = False) -> 'Duck':
        """Select * EXCLUDE (cols...)."""
        if not cols:
            return self
        for c in cols:
            validate_identifiers(c)
        resolved, missing = resolve_existing(self.columns, cols, skip_missing)
        if missing and not skip_missing:
            raise KeyError('Missing columns: ' + ', '.join(missing))
        if not resolved:
            return self
        out = Duck(self.con, self.rel.select(f'* EXCLUDE ({", ".join(resolved)})'))
        out._clear_table_target()
        return out

    def drop(self, *cols: str, skip_missing: bool = False) -> 'Duck':
        """Alias for exclude()."""
        return self.exclude(*cols, skip_missing=skip_missing)

    def replace(self, *, skip_missing: bool = False, **col_expr: str) -> 'Duck':
        """
        Replace existing columns with SQL expressions; preserve order.
        Example: duck.replace(total="price * qty")
        """
        if not col_expr:
            return self
        for tgt in col_expr.keys():
            validate_identifiers(tgt)
        cols = self.columns
        resolved_targets, missing = resolve_existing(cols, col_expr.keys(), skip_missing)
        if missing and not skip_missing:
            raise KeyError('Missing columns: ' + ', '.join(missing))
        cf_targets = {t.casefold() for t in resolved_targets}
        select_items = [
            (f'({col_expr[next(k for k in col_expr if k.casefold() == c.casefold())]}) AS {c}'
             if c.casefold() in cf_targets else c)
            for c in cols
        ]
        out = Duck(self.con, self.rel.select(', '.join(select_items)))
        out._clear_table_target()
        return out

    def transform_columns(
            self,
            *,
            skip_missing: bool = False,
            **transforms: str | Callable[[str], str],
    ) -> 'Duck':
        """
        Transform one or more existing columns using a single SELECT-projection.

        For each target `col=expr` (or callable), replaces that column in-place while
        preserving column order and ensuring the expression reads from the *original*
        input columns (no temp columns are created).

        Examples
        --------
        >>> duck.transform_columns(price="coalesce({col}, 0)")
        >>> duck.transform_columns(name="upper({col})", created_at=lambda c: f"date_trunc('day', {c})")
        """
        if not transforms:
            return self

        # Validate target identifiers
        for tgt in transforms.keys():
            validate_identifiers(tgt)

        cols = self.columns  # snapshot once
        resolved_targets, missing = resolve_existing(cols, transforms.keys(), skip_missing)
        if missing and not skip_missing:
            raise KeyError('Missing columns: ' + ', '.join(sorted(missing)))
        if not resolved_targets:
            return self

        # Build a mapping of target(lowercased) -> SQL expression using original column names
        expr_map: Dict[str, str] = {}
        for target_name in resolved_targets:
            spec = next(v for k, v in transforms.items() if k.casefold() == target_name.casefold())
            expr = spec(target_name) if callable(spec) else spec.format(col=target_name)
            expr_map[target_name.casefold()] = expr

        # Single SELECT that replaces targets in-place, preserving order
        select_items: List[str] = []
        for c in cols:
            key = c.casefold()
            if key in expr_map:
                select_items.append(f'({expr_map[key]}) AS {c}')
            else:
                select_items.append(c)

        out = Duck(self.con, self.rel.select(', '.join(select_items)))
        out._clear_table_target()
        return out

    def add_columns(self, overwrite: bool = False, **new_expr: str) -> 'Duck':
        """
        Append computed columns at the end. overwrite=True replaces collisions in-place.
        Example: duck.add_columns(price_with_tax="price * 1.08")
        """
        if not new_expr:
            return self
        for name in new_expr.keys():
            validate_identifiers(name)
        existing_cf = {c.casefold() for c in self.columns}
        collisions = [n for n in new_expr if n.casefold() in existing_cf]
        if collisions and not overwrite:
            raise ValueError('add_columns would overwrite: ' + ', '.join(collisions))

        cols = self.columns
        repl_cf = {n.casefold(): n for n in new_expr}
        select_items: List[str] = []
        for c in cols:
            if c.casefold() in repl_cf:
                expr = new_expr[repl_cf[c.casefold()]]
                select_items.append(f'({expr}) AS {c}')
            else:
                select_items.append(c)
        for name, expr in new_expr.items():
            if name.casefold() not in existing_cf:
                select_items.append(f'({expr}) AS {name}')
        out = Duck(self.con, self.rel.select(', '.join(select_items)))


        out._clear_table_target()
        return out

    def add_constants(self, overwrite: bool = False, **constants: Any) -> 'Duck':
        """
        Add constant columns to the relation.

        Example:
          duck.add_constants(env='prod', version=3)
        """
        if not constants:
            return self
        for name in constants:
            validate_identifiers(name)
        exprs = {name: _sql_literal(val) for name, val in constants.items()}
        return self.add_columns(overwrite=overwrite, **exprs)

    def coalesce_columns(self, drop_sources: bool = True, **sources_tuples: Iterable[str]) -> 'Duck':
        """target := coalesce(sources...), optionally dropping original source columns."""
        # ignore any stray kwargs like drop_sources if they were passed
        ops = [(target, tuple(srcs)) for target, srcs in sources_tuples.items() if target != 'drop_sources']
        if not ops:
            return self

        out = self
        for target, srcs in ops:
            validate_identifiers(target, *srcs)
            cols = out.columns           # resolve against the current output, not the original
            resolved, _ = resolve_existing(cols, srcs, skip_missing=False)
            expr = f'coalesce({", ".join(resolved)})'
            out = out.add_columns(**{target: expr})
            if drop_sources:
                out = out.drop(*srcs)

        return out



    def distinct(self) -> 'Duck':
        """Return relation with duplicate rows removed."""
        out = Duck(self.con, self.rel.distinct())
        out._clear_table_target()
        return out

    def filter(self, predicate_sql: str) -> 'Duck':
        """Return filtered relation using DuckDB SQL predicate."""
        out = Duck(self.con, self.rel.filter(predicate_sql))
        out._clear_table_target()
        return out

    def order_by(self, *order_items: str) -> 'Duck':
        """ORDER BY helper: duck.order_by("col1 ASC", "col2 DESC")."""
        if not order_items:
            return self
        out = Duck(self.con, self.rel.order(', '.join(order_items)))
        out._clear_table_target()
        return out

    def limit(self, n: int) -> 'Duck':
        """LIMIT n."""
        out = Duck(self.con, self.rel.limit(n))
        out._clear_table_target()
        return out

    def offset(self, n: int) -> 'Duck':
        """OFFSET n (implemented as LIMIT huge, offset)."""
        out = Duck(self.con, self.rel.limit(9223372036854775807, n))
        out._clear_table_target()
        return out

    def head(self, n: int = 5) -> 'Duck':
        """First n rows."""
        return self.limit(n)

    def tail(self, n: int = 5, *, order_by: Optional[str] = None) -> 'Duck':
        """
        Last n rows. If order_by is None, attempts to pick the first column
        and emulate tail deterministically.
        """
        ob = order_by or (self.columns[0] if self.columns else None)
        if ob:
            a = _reg1(self.con, self.rel)
            sql = (
                'SELECT * FROM (SELECT * FROM {a} ORDER BY {ob} DESC LIMIT {n}) '
                'ORDER BY {ob} ASC'
            ).format(a=a, ob=ob, n=n)
            rel = self.con.sql(sql)
            out = Duck(self.con, rel)
            out._clear_table_target()
            return out
        cnt = self.count_rows()
        off = max(0, cnt - n)
        out = Duck(self.con, self.rel.limit(9223372036854775807, off))
        out._clear_table_target()
        return out

    def sample(
            self,
            *,
            fraction: Optional[float] = None,
            n: Optional[int] = None,
            method: Literal['bernoulli', 'system'] = 'bernoulli',
            seed: Optional[int] = None,
    ) -> 'Duck':
        """Table sampling wrapper aligned with DuckDB docs."""
        if fraction is None and n is None:
            raise DuckTypeError('Provide fraction or n.')

        a = _reg1(self.con, self.rel)

        if n is not None:
            rows = int(n)
            if rows < 0:
                raise DuckTypeError('n must be >= 0')
            sql = f"SELECT * FROM {a} USING SAMPLE reservoir({rows} ROWS)"
            if seed is not None:
                sql += f" REPEATABLE ({int(seed)})"
        else:
            frac = float(fraction)
            if not (0.0 <= frac <= 1.0):
                frac = max(0.0, min(1.0, frac))
            pct = frac * 100.0

            if method == 'system':
                seed_part = f", {int(seed)}" if seed is not None else ""
                sql = f"SELECT * FROM {a} USING SAMPLE {pct}% (system{seed_part})"
            else:
                seed_part = f", {int(seed)}" if seed is not None else ""
                sql = f"SELECT * FROM {a} USING SAMPLE {pct} PERCENT (bernoulli{seed_part})"

        rel = self.con.sql(sql)
        out = Duck(self.con, rel)
        out._clear_table_target()
        return out

    # ---------- aggregate (GROUP BY ALL only) ----------
    def _resolve_singleton_or(self, column: Optional[str]) -> str:
        if column is not None:
            validate_identifiers(column)
            resolved, _ = resolve_existing(self.columns, [column], skip_missing=False)
            return resolved[0]
        if len(self.columns) != 1:
            raise ValueError('Column is required unless the relation has exactly one column.')
        return self.columns[0]

    def min_rel(self, column: Optional[str] = None) -> 'Duck':
        """SELECT min(col)."""
        col = self._resolve_singleton_or(column)
        a = _reg1(self.con, self.rel)
        rel = self.con.sql(f"SELECT min({col}) AS min_{col} FROM {a}")
        return Duck(self.con, rel)

    def max_rel(self, column: Optional[str] = None) -> 'Duck':
        """SELECT max(col)."""
        col = self._resolve_singleton_or(column)
        a = _reg1(self.con, self.rel)
        rel = self.con.sql(f"SELECT max({col}) AS max_{col} FROM {a}")
        return Duck(self.con, rel)

    def sum_rel(self, column: Optional[str] = None) -> 'Duck':
        """SELECT sum(col)."""
        col = self._resolve_singleton_or(column)
        a = _reg1(self.con, self.rel)
        rel = self.con.sql(f"SELECT sum({col}) AS sum_{col} FROM {a}")
        return Duck(self.con, rel)

    def avg_rel(self, column: Optional[str] = None) -> 'Duck':
        """SELECT avg(col)."""
        col = self._resolve_singleton_or(column)
        a = _reg1(self.con, self.rel)
        rel = self.con.sql(f"SELECT avg({col}) AS avg_{col} FROM {a}")
        return Duck(self.con, rel)

    def aggregate(
            self,
            *group_select: str,
            having: Optional[List[str]] = None,
            **agg_expressions: str,
    ) -> 'Duck':
        """
        GROUP BY ALL aggregation.
        Example: duck.aggregate("country", total="sum(amount)", having=["sum(amount) > 0"])
        """
        if not agg_expressions and not group_select:
            return self
        for g in group_select:
            validate_identifiers(g)
        for alias_name in agg_expressions.keys():
            validate_identifiers(alias_name)
        select_items: List[str] = []
        if group_select:
            resolved, _ = resolve_existing(self.columns, group_select, skip_missing=False)
            select_items.extend(resolved)
        select_items.extend(f'({expr}) AS {alias_name}' for alias_name, expr in agg_expressions.items())
        a = _reg1(self.con, self.rel)
        sql = 'SELECT {sel} FROM {a} GROUP BY ALL'.format(sel=', '.join(select_items), a=a)
        if having:
            sql += ' HAVING ' + ' AND '.join(f'({h})' for h in having)
        rel = self.con.sql(sql)
        out = Duck(self.con, rel)
        out._clear_table_target()
        return out

    def count_rows(self) -> int:
        """Fast COUNT(*) of the relation."""
        return int(self.aggregate(cnt="count(*)::BIGINT").fetch_list_int()[0])

 

    def fetch_list_int(self)->list[int]:
        return [int(v) for v, in self.rel.fetchall()]

    def fetch_list_str(self)->list[str]:
        return [str(v) for v, in self.rel.fetchall()]


    # ---------- list / NumPy ----------
    def fetch_list(
            self,
            expected_type: type | Tuple[type, ...],
            *,
            column: int = 0,
            allow_none: bool = True,
            collection_item_type: Optional[type | Tuple[type, ...]] = None,
            dict_enforce: Literal['both', 'keys', 'values', 'none'] = 'both',
            drop_nulls: bool = False,
    ) -> List[Any]:
        """
        Fetch one column as a list with optional nested validation.
        - If expected_type is a container (list/tuple/set/dict), we validate the container type.
        - If collection_item_type is set, we validate elements (or keys/values for dict).
        """
        vals = _fetch_column(self.rel, column=column)
        if not vals:
            return vals
        empty_val = _empty_for_expected_type(expected_type)
        out: List[Any] = []
        for v in vals:
            if v is None:
                if empty_val is not None:
                    out.append(empty_val.__class__())
                elif allow_none:
                    if not drop_nulls:
                        out.append(None)
                else:
                    raise TypeError('Encountered None but allow_none=False.')
                continue
            try:
                vv = _coerce_and_check(v, expected_type, allow_none=False)
            except TypeError:
                if isinstance(v, float) and isinstance(expected_type, tuple) and int in expected_type and float(v).is_integer():
                    vv = int(v)
                elif isinstance(v, int) and ((isinstance(expected_type, tuple) and float in expected_type) or expected_type is float):
                    vv = float(v)
                else:
                    raise
            if collection_item_type is not None and isinstance(vv, (list, tuple, set, dict)):
                if isinstance(vv, dict):
                    if dict_enforce in ('both', 'keys'):
                        keys = [_coerce_and_check(k, collection_item_type, allow_none=False) for k in vv.keys()]
                    else:
                        keys = list(vv.keys())
                    if dict_enforce in ('both', 'values'):
                        vals2 = [_coerce_and_check(val, collection_item_type, allow_none=False) for val in vv.values()]
                    else:
                        vals2 = list(vv.values())
                    vv = dict(zip(keys, vals2))
                elif isinstance(vv, list):
                    vv = [_coerce_and_check(it, collection_item_type, allow_none=False) for it in vv]
                elif isinstance(vv, tuple):
                    vv = tuple(_coerce_and_check(it, collection_item_type, allow_none=False) for it in vv)
                elif isinstance(vv, set):
                    vv = {_coerce_and_check(it, collection_item_type, allow_none=False) for it in vv}
            out.append(vv)
        if drop_nulls:
            out = [x for x in out if x is not None]
        return out

    def to_numpy_matrix(self, *, dtype: Optional[str] = None) -> Any:
        """Return the relation as a dense NumPy matrix (2D array)."""
        if _np is None:
            raise ImportError('NumPy is not available.')
        rows = self.rel.fetchall()
        return _np.array(rows, dtype=(dtype or object)) if rows else _np.empty((0, 0), dtype=dtype or object)

    # ---------- Arrow I/O ----------
    def to_arrow(self) -> Any:
        """Return a PyArrow table."""
        return self.rel.arrow()

    @classmethod
    def from_arrow(cls, con: duckdb.DuckDBPyConnection, arrow_table: Any) -> 'Duck':
        """Create a Duck from a PyArrow table."""
        return cls(con, duckdb.from_arrow(arrow_table))

    # ---------- NumPy (from) ----------
    @classmethod
    def from_numpy_matrix(
            cls,
            con: duckdb.DuckDBPyConnection,
            arr: Any,
            *,
            columns: Optional[Sequence[str]] = None,
    ) -> 'Duck':
        """
        Create from a 2D NumPy array (materialized through VALUES).
        For large data, prefer Arrow/Parquet to avoid SQL string blowup.
        """
        if _np is None:
            raise ImportError('NumPy is not available.')
        a = _np.asarray(arr)
        if a.ndim == 1:
            raise ValueError('Use Duck.from_numpy_1d(...) for 1D arrays.')
        if a.ndim != 2:
            raise ValueError('Expected a 2D array.')
        cols = list(columns) if columns else [f'c{i}' for i in range(a.shape[1])]
        for c in cols:
            validate_identifiers(c)
        py = a.tolist()
        values_rows = ', '.join('(' + ', '.join(repr(x) for x in row) + ')' for row in py)
        sql = f'SELECT {", ".join(cols)} FROM (VALUES {values_rows}) AS t({", ".join(cols)})'
        try:
            return cls(con, con.sql(sql))
        except duckdb.ParserException as e:
            e.add_note(sql)
            raise e

    @classmethod
    def from_numpy_1d(
            cls,
            con: duckdb.DuckDBPyConnection,
            arr: Any,
            *,
            column: str = "c0",
    ) -> "Duck":
        """
        Create from a 1D NumPy array.
        Note: Implementation uses a temporary materialization to avoid replacement scan binding issues.
        """
        if _np is None:
            raise ImportError("NumPy is not available.")
        _ = _np.asarray(arr).reshape(-1)
        validate_identifiers(column)
        # Simpler: register list directly via VALUES
        values_rows = ', '.join(f"({repr(x)})" for x in _)
        sql = f"SELECT {column} FROM (VALUES {values_rows}) AS t({column})"
        rel = con.sql(sql)
        return Duck(con, rel).materialize()

    # ---------- Schema helpers ----------
    def schema_of(self) -> Dict[str, str]:
        """Return {column: duckdb_sql_type}."""
        a = _reg1(self.con, self.rel)
        rel = self.con.sql('DESCRIBE SELECT * FROM {a}'.format(a=a))
        rows = rel.fetchall()
        return {r[0]: r[1] for r in rows}

    def dtypes_python(self, sample_rows: int = 1000) -> Dict[str, type]:
        """Best-effort Python types by sampling."""
        cols = self.columns
        limited = self.rel.limit(sample_rows).fetchall()
        if not limited:
            return {c: type(None) for c in cols}
        types: Dict[str, type] = {c: type(None) for c in cols}
        for row in limited:
            for i, v in enumerate(row):
                if v is not None:
                    types[cols[i]] = type(v)
        return types

    def assert_non_null(self, columns: Sequence[str]) -> None:
        """Raise ValueError if any NULL exists in any of the given columns."""
        if not columns:
            return
        resolved, _ = resolve_existing(self.columns, columns, skip_missing=False)
        pred = ' OR '.join(f'{c} IS NULL' for c in resolved)
        a = _reg1(self.con, self.rel)
        rel = self.con.sql('SELECT EXISTS(SELECT 1 FROM {a} WHERE {pred})'.format(a=a, pred=pred))
        any_null = _typed_cell(rel, (bool, int), allow_none=False)
        if bool(any_null):
            raise ValueError('NULLs found in columns: ' + ', '.join(resolved))

    def assert_unique(self, columns: Sequence[str]) -> None:
        """Raise ValueError if duplicates exist on the given columns."""
        if not columns:
            return
        resolved, _ = resolve_existing(self.columns, columns, skip_missing=False)
        cols_sql = ', '.join(resolved)
        a = _reg1(self.con, self.rel)
        sql = 'SELECT COUNT(*) - COUNT(DISTINCT {cols}) FROM {a}'.format(cols=cols_sql, a=a)
        rel = self.con.sql(sql)
        dup = _typed_cell(rel, int, allow_none=False)
        if int(dup) > 0:
            raise ValueError('Duplicates exist on: ' + ', '.join(resolved))

    # ========== I/O READERS (CSV / PARQUET / JSON / ODBC) ==========
    # The readers below accept explicit, typed parameters matching DuckDB's Python API
    # documentation. We *do not* forward **kwargs; instead we build SQL calls with
    # validated options to avoid silent mismatches.

    @classmethod
    def from_parquet(
        cls,
        con: duckdb.DuckDBPyConnection,
        sources: str | Path | Sequence[str | Path],
        *,
        partition_filters: Optional[
            Union[
                Sequence[Optional[Union[str, int, Sequence[Union[str, int]]]]],
                Mapping[str, Optional[Union[str, int, Sequence[Union[str, int]]]]],
            ]
        ] = None,
        hive_style: bool = False,
        where_sql: Optional[str] = None,
        # mirrored options
        binary_as_string: Optional[bool] = None,
        file_row_number: Optional[bool] = None,
        filename: Optional[bool] = None,
        hive_partitioning: Optional[bool] = None,
        union_by_name: Optional[bool] = None,
        compression: Optional[ParquetCompression] = None,
    ) -> 'Duck':
        """
        Create from Parquet (files/list/dir).

        Parameters mirror DuckDB's connection.from_parquet/read_parquet options:
          - binary_as_string: interpret BINARY as VARCHAR
          - file_row_number: include per-file row number column
          - filename: include source filename column
          - hive_partitioning: enable Hive-style directory parsing
          - union_by_name: align columns by name across files
          - compression: override read-time codec ('snappy','zstd',...)
        """
        files = _collect_paths(sources, ('.parquet',), partition_filters=partition_filters, hive_style=hive_style)
        if not files:
            raise DuckIOError('No Parquet files matched the given sources/filters.')

        def _opt(name: str, val: object | None, *, is_bool: bool = False) -> str:
            if val is None:
                return ''
            if is_bool:
                return f", {name}={'TRUE' if bool(val) else 'FALSE'}"
            return f", {name}={_sql_literal(val)}"

        opt_sql = ''
        opt_sql += _opt('BINARY_AS_STRING', binary_as_string, is_bool=True)
        opt_sql += _opt('FILE_ROW_NUMBER', file_row_number, is_bool=True)
        opt_sql += _opt('FILENAME', filename, is_bool=True)
        opt_sql += _opt('HIVE_PARTITIONING', hive_partitioning, is_bool=True)
        opt_sql += _opt('UNION_BY_NAME', union_by_name, is_bool=True)
        opt_sql += _opt('COMPRESSION', compression)

        try:
            files_sql = '[' + ', '.join("'" + f.replace("'", "''") + "'" for f in files) + ']'
            rel = con.sql(f"SELECT * FROM read_parquet({files_sql}{opt_sql})")
            if where_sql:
                rel = rel.filter(where_sql)
            return cls(con, rel)
        except Exception as e:  # pragma: no cover
            raise DuckIOError(str(e)) from e

    @classmethod
    def from_csv(
        cls,
        con: duckdb.DuckDBPyConnection,
        sources: str | Path | Sequence[str | Path],
        *,
        partition_filters: Optional[
            Union[
                Sequence[Optional[Union[str, int, Sequence[Union[str, int]]]]],
                Mapping[str, Optional[Union[str, int, Sequence[Union[str, int]]]]],
            ]
        ] = None,
        hive_style: bool = False,
        where_sql: Optional[str] = None,
        # documented read_csv_auto / from_csv_auto options (explicit & typed)
        header: bool | int | None = None,
        compression: Optional[CsvCompression] = None,
        sep: Optional[str] = None,
        delimiter: Optional[str] = None,
        dtype: Optional[Dict[str, str] | List[str]] = None,
        na_values: Optional[str | List[str]] = None,
        skiprows: Optional[int] = None,
        quotechar: Optional[str] = None,
        escapechar: Optional[str] = None,
        encoding: Optional[Encoding] = None,
        parallel: Optional[bool] = None,
        date_format: Optional[str] = None,
        timestamp_format: Optional[str] = None,
        sample_size: Optional[int] = None,
        all_varchar: Optional[bool] = None,
        normalize_names: Optional[bool] = None,
        null_padding: Optional[bool] = None,
        names: Optional[List[str]] = None,
        lineterminator: Optional[str] = None,
        columns: Optional[Dict[str, str]] = None,
        auto_type_candidates: Optional[List[str]] = None,
        max_line_size: Optional[int] = None,
        ignore_errors: Optional[bool] = None,
        store_rejects: Optional[bool] = None,
        rejects_table: Optional[str] = None,
        rejects_scan: Optional[str] = None,
        rejects_limit: Optional[int] = None,
        force_not_null: Optional[List[str]] = None,
        buffer_size: Optional[int] = None,
        decimal: Optional[str] = None,
        allow_quoted_nulls: Optional[bool] = None,
        filename: Optional[bool | str] = None,
        hive_partitioning: Optional[bool] = None,
        union_by_name: Optional[bool] = None,
        hive_types: Optional[Dict[str, str]] = None,
        hive_types_autocast: Optional[bool] = None,
    ) -> 'Duck':
        """
        Create from CSV (files/list/dir) using DuckDB's read_csv_auto.

        All options are explicit and typed; we translate them into SQL options for
        read_csv_auto(...). A few notes:
          - 'sep' and 'delimiter' are aliases; if both are provided, 'delimiter' wins.
          - 'header' accepts bool or row index; None means no header inference override.
          - 'dtype' accepts either a list[str] or mapping {column: type_string}.
          - 'filename' can be True/False or a specific filename to include.
        """
        extensions = ('.csv', '.csv.gz', '.csv.zst')
        files = _collect_paths(sources, extensions, partition_filters=partition_filters, hive_style=hive_style)
        if not files:
            raise DuckIOError('No CSV files matched the given sources/filters.')

        def _opt(name: str, val: object | None, *, is_bool: bool = False) -> str:
            if val is None:
                return ''
            if is_bool:
                return f", {name}={'TRUE' if bool(val) else 'FALSE'}"
            return f", {name}={_sql_literal(val)}"

        # delimiter precedence
        actual_delim = delimiter if delimiter is not None else sep

        opt_sql = ''
        opt_sql += _opt('HEADER', header)
        opt_sql += _opt('COMPRESSION', compression)
        opt_sql += _opt('DELIM', actual_delim)  # DuckDB option name is DELIM/DELIMITER
        opt_sql += _opt('DTYPE', dtype)
        opt_sql += _opt('NA_VALUES', na_values)
        opt_sql += _opt('SKIP', skiprows)
        opt_sql += _opt('QUOTE', quotechar)
        opt_sql += _opt('ESCAPE', escapechar)
        opt_sql += _opt('ENCODING', encoding)
        opt_sql += _opt('PARALLEL', parallel, is_bool=True)
        opt_sql += _opt('DATEFORMAT', date_format)
        opt_sql += _opt('TIMESTAMPFORMAT', timestamp_format)
        opt_sql += _opt('SAMPLE_SIZE', sample_size)
        opt_sql += _opt('ALL_VARCHAR', all_varchar, is_bool=True)
        opt_sql += _opt('NORMALIZE_NAMES', normalize_names, is_bool=True)
        opt_sql += _opt('NULL_PADDING', null_padding, is_bool=True)
        opt_sql += _opt('NAMES', names)
        opt_sql += _opt('NEWLINE', lineterminator)
        opt_sql += _opt('COLUMNS', columns)
        opt_sql += _opt('AUTO_TYPE_CANDIDATES', auto_type_candidates)
        opt_sql += _opt('MAX_LINE_SIZE', max_line_size)
        opt_sql += _opt('IGNORE_ERRORS', ignore_errors, is_bool=True)
        opt_sql += _opt('STORE_REJECTS', store_rejects, is_bool=True)
        opt_sql += _opt('REJECTS_TABLE', rejects_table)
        opt_sql += _opt('REJECTS_SCAN', rejects_scan)
        opt_sql += _opt('REJECTS_LIMIT', rejects_limit)
        opt_sql += _opt('FORCE_NOT_NULL', force_not_null)
        opt_sql += _opt('BUFFER_SIZE', buffer_size)
        opt_sql += _opt('DECIMAL', decimal)
        opt_sql += _opt('ALLOW_QUOTED_NULLS', allow_quoted_nulls, is_bool=True)
        opt_sql += _opt('FILENAME', filename)
        opt_sql += _opt('HIVE_PARTITIONING', hive_partitioning, is_bool=True)
        opt_sql += _opt('UNION_BY_NAME', union_by_name, is_bool=True)
        opt_sql += _opt('HIVE_TYPES', hive_types)
        opt_sql += _opt('HIVE_TYPES_AUTOCAST', hive_types_autocast, is_bool=True)

        try:
            files_sql = '[' + ', '.join("'" + f.replace("'", "''") + "'" for f in files) + ']'
            rel = con.sql(f"SELECT * FROM read_csv_auto({files_sql}{opt_sql})")
            if where_sql:
                rel = rel.filter(where_sql)
            return cls(con, rel)
        except Exception as e:  # pragma: no cover
            raise DuckIOError(str(e)) from e

    @classmethod
    def from_json(
        cls,
        con: duckdb.DuckDBPyConnection,
        sources: str | Path | Sequence[str | Path],
        *,
        partition_filters: Optional[
            Union[
                Sequence[Optional[Union[str, int, Sequence[Union[str, int]]]]],
                Mapping[str, Optional[Union[str, int, Sequence[Union[str, int]]]]],
            ]
        ] = None,
        hive_style: bool = False,
        json_mode: Literal['json', 'jsonl', 'auto'] = 'auto',
        where_sql: Optional[str] = None,
        # documented read_json options
        columns: Optional[object] = None,
        sample_size: Optional[int] = None,
        maximum_depth: Optional[int] = None,
        records: Optional[JsonRecords] = None,
        format: Optional[JsonFormat] = None,
        date_format: Optional[str] = None,
        timestamp_format: Optional[str] = None,
        compression: Optional[CsvCompression] = None,  # common compression names
        maximum_object_size: Optional[int] = None,
        ignore_errors: Optional[bool] = None,
        convert_strings_to_integers: Optional[bool] = None,
        field_appearance_threshold: Optional[float] = None,
        map_inference_threshold: Optional[float] = None,
        maximum_sample_files: Optional[int] = None,
        filename: Optional[bool] = None,
        hive_partitioning: Optional[bool] = None,
        union_by_name: Optional[bool] = None,
        hive_types: Optional[object] = None,
        hive_types_autocast: Optional[bool] = None,
    ) -> 'Duck':
        """
        Create from JSON (files/list/dir).

        Notes:
          - json_mode chooses between read_json (for normal JSON) and read_json_auto (for JSONL/auto).
          - Many JSON options are engine-tuned; we pass them through when provided.
        """
        extensions = ('.json', '.jsonl', '.json.gz', '.jsonl.gz', '.json.zst', '.jsonl.zst')
        files = _collect_paths(sources, extensions, partition_filters=partition_filters, hive_style=hive_style)
        if not files:
            raise DuckIOError('No JSON files matched the given sources/filters.')

        tf = "read_json_auto" if json_mode in ("auto", "jsonl") else "read_json"

        def _opt(name: str, val: object | None, *, is_bool: bool = False) -> str:
            if val is None:
                return ''
            if is_bool:
                return f", {name}={'TRUE' if bool(val) else 'FALSE'}"
            return f", {name}={_sql_literal(val)}"

        opt_sql = ''
        opt_sql += _opt('COLUMNS', columns)
        opt_sql += _opt('SAMPLE_SIZE', sample_size)
        opt_sql += _opt('MAXIMUM_DEPTH', maximum_depth)
        opt_sql += _opt('RECORDS', records)
        opt_sql += _opt('FORMAT', format)
        opt_sql += _opt('DATE_FORMAT', date_format)
        opt_sql += _opt('TIMESTAMP_FORMAT', timestamp_format)
        opt_sql += _opt('COMPRESSION', compression)
        opt_sql += _opt('MAXIMUM_OBJECT_SIZE', maximum_object_size)
        opt_sql += _opt('IGNORE_ERRORS', ignore_errors, is_bool=True)
        opt_sql += _opt('CONVERT_STRINGS_TO_INTEGERS', convert_strings_to_integers, is_bool=True)
        opt_sql += _opt('FIELD_APPEARANCE_THRESHOLD', field_appearance_threshold)
        opt_sql += _opt('MAP_INFERENCE_THRESHOLD', map_inference_threshold)
        opt_sql += _opt('MAXIMUM_SAMPLE_FILES', maximum_sample_files)
        opt_sql += _opt('FILENAME', filename, is_bool=True)
        opt_sql += _opt('HIVE_PARTITIONING', hive_partitioning, is_bool=True)
        opt_sql += _opt('UNION_BY_NAME', union_by_name, is_bool=True)
        opt_sql += _opt('HIVE_TYPES', hive_types)
        opt_sql += _opt('HIVE_TYPES_AUTOCAST', hive_types_autocast, is_bool=True)

        try:
            files_sql = '[' + ', '.join("'" + f.replace("'", "''") + "'" for f in files) + ']'
            rel = con.sql(f"SELECT * FROM {tf}({files_sql}{opt_sql})")
            if where_sql:
                rel = rel.filter(where_sql)
            return cls(con, rel)
        except Exception as e:  # pragma: no cover
            raise DuckIOError(str(e)) from e

    @classmethod
    def from_odbc_query(
        cls,
        con: duckdb.DuckDBPyConnection,
        query: str,
        *,
        # Connection selection (priority 1→3)
        raw_connection_string: str | None = None,      # use as-is
        dsn: str | None = None,                        # DSN of any driver
        # SQL Server (non-DSN)
        server: str | None = None,
        database: str | None = None,
        driver: Literal[
            # SQL Server
            "ODBC Driver 18 for SQL Server",
            "ODBC Driver 17 for SQL Server",
            "SQL Server",
            "SQL Server Native Client 11.0",
            # Excel
            "Microsoft Excel Driver (*.xls, *.xlsx, *.xlsm, *.xlsb)",
        ] = "ODBC Driver 17 for SQL Server",

        # Auth:
        # - If use_trusted_connection is None: auto ⇒ use SQL auth when username is provided; else Trusted_Connection
        use_trusted_connection: bool | None = None,
        username: str | None = None,
        keyring_service: str | None = None,

        # SQL Server encryption knobs:
        # - Driver 18 defaults to Encrypt=Yes, TrustServerCertificate=Yes
        # - Driver 17/legacy: not included unless explicitly set
        encrypt: str | None = None,
        trust_server_certificate: str | None = None,

        # Excel options (when Excel driver is selected)
        excel_path: str | None = None,
        excel_read_only: bool = True,
        excel_extended_properties: str | None = None,

        # nanodbc read options
        all_varchar: bool = DEFAULT_ODBC_ALL_VARCHAR,
        encoding: str = DEFAULT_ODBC_ENCODING,
        read_only: bool = DEFAULT_ODBC_READ_ONLY,

        # Extra ODBC properties
        odbc_extra: Mapping[str, str] | None = None,
    ) -> "Duck":
        """
        Execute `query` through DuckDB's nanodbc and return a Duck.

        Selection precedence:
          1) raw_connection_string
          2) DSN=...
          3) Driver-specific:
             - SQL Server: server+database [Trusted_Connection or SQL auth via keyring]
             - Excel: excel_path (+ Extended Properties)

        Defaults:
          - Password vs Trusted: if `username` provided → SQL auth; otherwise Trusted_Connection=Yes
          - Encryption: Driver 18 ⇒ Encrypt=Yes & TrustServerCertificate=Yes by default;
                        Driver 17/legacy ⇒ omitted unless explicitly set
        """
        _ensure_nanoodbc_loaded(con)

        # 1) raw string
        if raw_connection_string:
            odbc_str = raw_connection_string

        # 2) DSN
        elif dsn:
            auto_trusted = (username is None) if use_trusted_connection is None else use_trusted_connection
            if auto_trusted:
                odbc_str = _build_conn_str_dsn(dsn=dsn, use_trusted_connection=True, extra=odbc_extra)
            else:
                if not username:
                    raise ValueError("username is required when not using Trusted_Connection.")
                service = keyring_service or dsn
                def _mk(u: str, p: str) -> str:
                    return _build_conn_str_dsn(
                        dsn=dsn, use_trusted_connection=False, username=u, password=p, extra=odbc_extra
                    )
                password = _get_or_ask_password(
                    service=service, username=username, con=con,
                    precheck=True, build_conn_for_verify=_mk,
                )
                odbc_str = _mk(username, password)

        # 3) Driver-specific
        else:
            if driver == "Microsoft Excel Driver (*.xls, *.xlsx, *.xlsm, *.xlsb)":
                if not excel_path and not odbc_extra and not server:
                    raise ValueError("excel_path is required for Excel driver (or use DSN/raw_connection_string).")
                odbc_str = _build_conn_str_excel(
                    path=excel_path or "",
                    driver=driver,
                    read_only=excel_read_only,
                    extended_properties=excel_extended_properties,
                    extra=odbc_extra,
                )
            else:
                # SQL Server mode
                if not (server and database):
                    raise ValueError("server and database are required for SQL Server mode.")
                auto_trusted = (username is None) if use_trusted_connection is None else use_trusted_connection

                # driver-aware encryption defaults
                if encrypt is None and driver == "ODBC Driver 18 for SQL Server":
                    enc_eff = "Yes"
                else:
                    enc_eff = encrypt  # None ⇒ not included
                if trust_server_certificate is None and driver == "ODBC Driver 18 for SQL Server":
                    tsc_eff = "Yes"
                else:
                    tsc_eff = trust_server_certificate

                if auto_trusted:
                    odbc_str = _build_conn_str_sqlserver(
                        server=server, database=database, driver=driver,
                        use_trusted_connection=True,
                        encrypt=enc_eff, trust_server_certificate=tsc_eff,
                        extra=odbc_extra,
                    )
                else:
                    if not username:
                        raise ValueError("username is required when not using Trusted_Connection.")
                    service = keyring_service or server
                    
                    def _mk(u: str, p: str) -> str:
                        return _build_conn_str_sqlserver(
                            server=server, database=database, driver=driver,
                            use_trusted_connection=False, username=u, password=p,
                            encrypt=enc_eff, trust_server_certificate=tsc_eff,
                            extra=odbc_extra,
                      )
                    password = _get_or_ask_password(
                        service=service, username=username, con=con,
                        precheck=True, build_conn_for_verify=_mk,
                    )
                    odbc_str = _mk(username, password)

        # Execute using nanodbc
        rel = con.sql(
            "SELECT * FROM odbc_query("
            f"connection={_sql_literal(odbc_str)}, "
            f"query={_sql_literal(query)}, "
            f"all_varchar={str(all_varchar).lower()}, "
            f"encoding={_sql_literal(encoding)}, "
            f"read_only={str(read_only).lower()}"
            ")"
        )
        return cls(con, rel)

    # ========== I/O WRITERS (CSV / PARQUET) ==========

    def write_parquet(
        self,
        path: str | Path,
        *,
        compression: ParquetCompression | None = 'zstd',
        field_ids: object | None = None,           # STRUCT or 'auto' per DuckDB
        row_group_size_bytes: int | str | None = None,  # int bytes or human string (e.g., '2MB')
        row_group_size: int | None = None,         # row count (ignored unless preserve_insertion_order=false)
        overwrite: bool | None = None,
        per_thread_output: bool | None = None,
        use_tmp_file: bool | None = None,
        partition_by: list[str] | None = None,
        write_partition_columns: bool | None = None,
        append: bool | None = None,
    ) -> None:
        """
        Write to Parquet using DuckDB's PyRelation.to_parquet() options.

        Parameters (selected):
          - compression: 'uncompressed'|'snappy'|'gzip'|'zstd'|'brotli'|'lz4'|'lz4_raw'
          - field_ids: STRUCT or 'auto' (engine infers nested field IDs)
          - row_group_size_bytes: int bytes or human-readable string ('2MB')
          - row_group_size: int rows (see DuckDB docs re: preserve_insertion_order)
          - overwrite: overwrite existing (only effects with partition_by)
          - per_thread_output: write one file per thread
          - use_tmp_file: write to temp then rename
          - partition_by: list of columns to partition
          - write_partition_columns: whether to include partition cols in files
          - append: avoid overwriting when filename pattern collides (with partitioning)
        """
        if partition_by:
            for c in partition_by:
                validate_identifiers(c)

        kwargs: Dict[str, Any] = {}
        for k, v in dict(
            compression=compression,
            field_ids=field_ids,
            row_group_size_bytes=row_group_size_bytes,
            row_group_size=row_group_size,
            overwrite=overwrite,
            per_thread_output=per_thread_output,
            use_tmp_file=use_tmp_file,
            partition_by=partition_by,
            write_partition_columns=write_partition_columns,
            append=append,
        ).items():
            if v is not None:
                kwargs[k] = v

        try:
            self.rel.to_parquet(_as_path_str(path), **kwargs)  # type: ignore[arg-type]
        except Exception as e:  # pragma: no cover
            raise DuckIOError(str(e)) from e

    def write_parquet_partitioned(
        self,
        root: str | Path,
        *,
        partition_keys: Sequence[str],
        hive_style: bool = True,
        mode: Literal['overwrite', 'append'] = 'overwrite',
        filename_factory: Optional[Callable[[Dict[str, Any]], str]] = None,
        append_antijoin_keys: Optional[Sequence[str]] = None,
        compression: ParquetCompression | None = 'zstd',
    ) -> None:
        """
        Manual partitioned writer (no DuckDB PARTITION_BY). Creates folders per partition,
        optionally anti-joining with existing data during append.
        """
        if not partition_keys:
            raise DuckTypeError('partition_keys must be non-empty.')
        for k in partition_keys:
            validate_identifiers(k)
        if append_antijoin_keys:
            missing = [k for k in append_antijoin_keys if k not in partition_keys]
            if missing:
                raise DuckTypeError('append_antijoin_keys must be a subset of partition_keys.')

        root_path = Path(root)
        root_path.mkdir(parents=True, exist_ok=True)

        pk = ', '.join(partition_keys)
        combos = self.rel.select(pk).distinct().fetchall()

        for combo in combos:
            parts = {partition_keys[i]: combo[i] for i in range(len(partition_keys))}
            if hive_style:
                sub_directories = [f'{k}={parts[k]}' for k in partition_keys]
            else:
                sub_directories = [safe_filename(parts[k]) for k in partition_keys]
            out_dir = root_path.joinpath(*map(str, sub_directories))
            out_dir.mkdir(parents=True, exist_ok=True)
            file_name = (filename_factory(parts) if filename_factory else f'part-{uuid.uuid4().hex}.parquet')
            out_file = out_dir / file_name

            pred = ' AND '.join(f'{k} = {repr(parts[k])}' for k in partition_keys)
            rel_part = self.rel.filter(pred)

            if mode == 'append' and append_antijoin_keys:
                try:
                    existing = Duck.from_parquet(self.con, out_dir, partition_filters=None).rel
                    a, b = _reg2(self.con, rel_part, existing)
                    on = ' AND '.join(f'{a}.{k}={b}.{k}' for k in append_antijoin_keys)
                    sql = 'SELECT {a}.* FROM {a} WHERE NOT EXISTS (SELECT 1 FROM {b} WHERE {on})'.format(
                        a=a, b=b, on=on
                    )
                    rel_part = self.con.sql(sql)
                except DuckIOError:
                    pass  # nothing written yet

            Duck(self.con, rel_part).write_parquet(out_file, compression=compression)

    def write_csv(
        self,
        path: str | Path,
        *,
        sep: str | None = None,                     # defaults to ','
        na_rep: str | None = None,
        header: bool | None = None,                 # default True in DuckDB
        quotechar: str | None = None,               # default '"'
        escapechar: str | None = None,
        date_format: str | None = None,
        timestamp_format: str | None = None,
        quoting: CsvQuoting | None = None,          # 'minimal'|'all'|'none'|'nonnumeric'
        encoding: Encoding | None = None,           # default 'utf-8'
        compression: CsvCompression | None = None,  # 'auto'|'gzip'|'bz2'|'zstd'
        overwrite: bool | None = None,
        per_thread_output: bool | None = None,
        use_tmp_file: bool | None = None,
        partition_by: list[str] | None = None,
        write_partition_columns: bool | None = None,
    ) -> None:
        """
        Write to CSV using DuckDB's PyRelation.to_csv() options, with typed/ergonomic parameters.

        Important:
          - `quoting` uses lowercase string literals and is translated to `csv.QUOTE_*`.
          - `partition_by` creates directory partitioning; `overwrite` only affects partitioned writes.
        """
        if partition_by:
            for c in partition_by:
                validate_identifiers(c)

        kwargs: Dict[str, Any] = {}
        for k, v in dict(
            sep=sep,
            na_rep=na_rep,
            header=header,
            quotechar=quotechar,
            escapechar=escapechar,
            date_format=date_format,
            timestamp_format=timestamp_format,
            quoting=(_QUOTING_MAP[quoting] if quoting is not None else None),
            encoding=encoding,
            compression=compression,
            overwrite=overwrite,
            per_thread_output=per_thread_output,
            use_tmp_file=use_tmp_file,
            partition_by=partition_by,
            write_partition_columns=write_partition_columns,
        ).items():
            if v is not None:
                kwargs[k] = v

        try:
            self.rel.to_csv(_as_path_str(path), **kwargs)  # type: ignore[arg-type]
        except Exception as e:  # pragma: no cover
            raise DuckIOError(str(e)) from e

    # ---------- Tables (targeting mode) ----------
    @classmethod
    def from_table(cls, con: duckdb.DuckDBPyConnection, table: str) -> 'Duck':
        """Create a relation from an existing table name (no targeting semantics)."""
        validate_identifiers(table)
        return cls(con, con.table(table))

    @classmethod
    def table(cls, con: duckdb.DuckDBPyConnection, table: str) -> 'Duck':
        """
        Begin in table-targeting mode for an existing table; raises if not found.

        Table-targeting mode allows mutating helpers (e.g., insert_into/delete_from) that require
        a concrete base table name.
        """
        validate_identifiers(table)
        try:
            rel = con.table(table)
        except Exception as e:
            raise DuckTableTargetError(f'Table not found: {table}') from e
        return cls(con, rel, table_name=table)

    def to_table(self, name: str, overwrite: bool = False) -> None:
        """CREATE TABLE AS SELECT with optional overwrite (DROP TABLE IF EXISTS)."""
        validate_identifiers(name)
        if overwrite:
            self.con.execute(f'DROP TABLE IF EXISTS {name}')
        a = _reg1(self.con, self.rel)
        self.con.execute('CREATE TABLE {name} AS SELECT * FROM {a}'.format(name=name, a=a))

    def rename_table(self, new_name: str) -> 'Duck':
        """Rename targeted table; remains in targeting mode with new name."""
        old = self._require_table_target()
        validate_identifiers(new_name)
        self.con.execute(f'ALTER TABLE {old} RENAME TO {new_name}')
        self._table_name = new_name
        return self

    def _require_table_target(self) -> str:
        if not self._table_name:
            raise DuckTableTargetError('This operation requires table-targeting mode. Use Duck.table(...).')
        return self._table_name

    def add_index(self, index_name: str, column_list: Sequence[str], unique: bool = False) -> 'Duck':
        """CREATE [UNIQUE] INDEX on targeted table."""
        table = self._require_table_target()
        for c in column_list:
            validate_identifiers(c)
        validate_identifiers(index_name)
        unique_sql = 'UNIQUE ' if unique else ''
        cols_sql = ', '.join(column_list)
        self.con.execute(f'CREATE {unique_sql}INDEX {index_name} ON {table} ({cols_sql})')
        return self

    def drop_index(self, index_name: str) -> 'Duck':
        """DROP INDEX IF EXISTS on targeted table."""
        self._require_table_target()
        validate_identifiers(index_name)
        self.con.execute(f'DROP INDEX IF EXISTS {index_name}')
        return self

    def insert_into(self, other: 'Duck', columns: Optional[Sequence[str]] = None) -> 'Duck':
        """INSERT INTO targeted table from another Duck relation."""
        table = self._require_table_target()
        if columns:
            for c in columns:
                validate_identifiers(c)
            cols_sql = '(' + ', '.join(columns) + ')'
        else:
            cols_sql = ''
        a = _reg1(self.con, other.rel)
        self.con.execute('INSERT INTO {t} {cols} SELECT * FROM {a}'.format(t=table, cols=cols_sql, a=a))
        return self

    def delete_from(self, where: str) -> 'Duck':
        """DELETE FROM targeted table with predicate SQL."""
        table = self._require_table_target()
        self.con.execute(f'DELETE FROM {table} WHERE {where}')
        return self

    def truncate(self) -> 'Duck':
        """DELETE all rows from targeted table (TRUNCATE-like semantics)."""
        table = self._require_table_target()
        self.con.execute(f'DELETE FROM {table}')
        return self

    # ---------- Joins with optional partitioning & progress callback ----------
    def _join_core(
        self,
        how: Literal['NATURAL', 'INNER', 'LEFT', 'SEMI', 'ANTI', 'FULL', 'CROSS'],
        other: 'Duck',
        *,
        unnatural_pairs: Optional[Dict[str, str]] = None,
        partition_by: Optional[Sequence[str]] = None,
        max_unions: int = 500,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> 'Duck':

        if how == 'CROSS':
            a, b = _reg2(self.con, self.rel, other.rel)
            sql = f'SELECT * FROM {a} CROSS JOIN {b}'
            return Duck(self.con, self.con.sql(sql))
        else:
            on_shared = False if (unnatural_pairs is not None) else True

        self_cols = [v.lower() for v in self.columns]
        other_cols = [v.lower() for v in other.columns]
        shared = sorted(set(self_cols).intersection(other_cols), key=str)

        # Determine join column pairs
        pairs: List[Tuple[str, str]]
        if how == 'NATURAL' or on_shared:
            allow_empty = (how in ('SEMI', 'ANTI'))
            if not shared and not allow_empty:
                raise DuckTypeError(
                    'No shared columns for NATURAL/on_shared join.'
                    f"\nSelf: {self_cols}\nOther: {other_cols}"
                )
            pairs = [(c, c) for c in shared]
        else:
            up = unnatural_pairs or {}
            for k, v in up.items():
                validate_identifiers(k, v)
            pairs = list(up.items()) if up else ([(c, c) for c in shared] if shared else [])
            if not pairs and how not in ('SEMI', 'ANTI'):
                raise DuckTypeError('Join requires ON columns (no shared columns and no unnatural_pairs).')

        def join_sql_for(a_name: str, b_name: str) -> str:
            if how == 'NATURAL':
                return f'SELECT * FROM {a_name} NATURAL JOIN {b_name}'
            if how == 'SEMI':
                on_clause = ' AND '.join(f'{a_name}.{l} = {b_name}.{r}' for l, r in pairs) if pairs else 'TRUE'
                return f'SELECT {a_name}.* FROM {a_name} WHERE EXISTS (SELECT 1 FROM {b_name} WHERE {on_clause})'
            if how == 'ANTI':
                on_clause = ' AND '.join(f'{a_name}.{l} = {b_name}.{r}' for l, r in pairs) if pairs else 'TRUE'
                return f'SELECT {a_name}.* FROM {a_name} WHERE NOT EXISTS (SELECT 1 FROM {b_name} WHERE {on_clause})'

            kind = {'INNER': 'INNER', 'LEFT': 'LEFT', 'FULL': 'FULL'}[how]

            # Use USING(...) when all join pairs have identical names to de-duplicate join keys
            if pairs and all(l == r for l, r in pairs):
                using_cols = ', '.join(l for l, _ in pairs)
                return f'SELECT * FROM {a_name} {kind} JOIN {b_name} USING ({using_cols})'

            # Fallback: ON ... (keeps both key copies)
            on_clause = ' AND '.join(f'{a_name}.{l} = {b_name}.{r}' for l, r in pairs)
            return f'SELECT * FROM {a_name} {kind} JOIN {b_name} ON {on_clause}'

        # Non-partitioned path
        if not partition_by:
            a, b = _reg2(self.con, self.rel, other.rel)
            rel = self.con.sql(join_sql_for(a, b))
            return Duck(self.con, rel)

        # Helpers for partitioned path
        def distinct_vals(rel: duckdb.DuckDBPyRelation, keys: Sequence[str]) -> List[Tuple[Any, ...]]:
            cols = ', '.join(keys)
            name = _reg1(self.con, rel)
            return self.con.sql(f'SELECT DISTINCT {cols} FROM {name}').fetchall()

        # --- Phase 1: choose a workable partition key width ---
        chosen_keys: Optional[List[str]] = None
        left_set: Optional[set[Tuple[Any, ...]]] = None
        right_set: Optional[set[Tuple[Any, ...]]] = None

        for width in range(len(partition_by), 0, -1):
            keys = list(partition_by)[:width]

            l_vals = set(tuple(r) for r in distinct_vals(self.rel, keys))
            r_vals = set(tuple(r) for r in distinct_vals(other.rel, keys))

            if not l_vals:
                a0 = _reg1(self.con, self.rel.limit(0))
                if how == 'NATURAL':
                    b0 = _reg1(self.con, other.rel.limit(0))
                    return Duck(self.con, self.con.sql(join_sql_for(a0, b0)))
                return Duck(self.con, self.rel.limit(0))

            if how == 'ANTI':
                total = len(l_vals)
                if total <= max_unions:
                    chosen_keys = keys
                    left_set = l_vals
                    right_set = r_vals
                    break
            else:
                combos = l_vals & r_vals
                total = len(combos)
                if total == 0:
                    a0 = _reg1(self.con, self.rel.limit(0))
                    b0 = _reg1(self.con, other.rel.limit(0))
                    return Duck(self.con, self.con.sql(join_sql_for(a0, b0)))
                if total <= max_unions:
                    chosen_keys = keys
                    left_set = l_vals
                    right_set = r_vals
                    break

        if chosen_keys is None or left_set is None or right_set is None:
            a, b = _reg2(self.con, self.rel, other.rel)
            rel = self.con.sql(join_sql_for(a, b))
            return Duck(self.con, rel)

        # --- Phase 2: per-partition execution and UNION ALL BY NAME ---
        rel_out: Optional[duckdb.DuckDBPyRelation] = None

        if how == 'ANTI':
            keys_overlap = sorted(left_set & right_set)
            keys_left_only = sorted(left_set - right_set)

            total = len(keys_left_only) + len(keys_overlap)
            step = 0

            for combo in keys_left_only:
                step += 1
                if progress_callback:
                    try: progress_callback(step, total)
                    except Exception: pass

                pred = ' AND '.join(f"{k} = {repr(combo[j])}" for j, k in enumerate(chosen_keys))
                a_part = self.rel.filter(pred)
                piece = a_part
                rel_out = piece if rel_out is None else self.con.sql(
                    f"SELECT * FROM {_reg1(self.con, rel_out)} UNION ALL BY NAME SELECT * FROM {_reg1(self.con, piece)}"
                )

            for combo in keys_overlap:
                step += 1
                if progress_callback:
                    try: progress_callback(step, total)
                    except Exception: pass

                pred = ' AND '.join(f"{k} = {repr(combo[j])}" for j, k in enumerate(chosen_keys))
                a_part = self.rel.filter(pred)
                b_part = other.rel.filter(pred)
                a_name, b_name = _reg2(self.con, a_part, b_part)
                piece = self.con.sql(join_sql_for(a_name, b_name))

                rel_out = piece if rel_out is None else self.con.sql(
                    f"SELECT * FROM {_reg1(self.con, rel_out)} UNION ALL BY NAME SELECT * FROM {_reg1(self.con, piece)}"
                )

            return Duck(self.con, rel_out if rel_out is not None else self.rel.limit(0))

        chosen_combos = sorted(left_set & right_set)
        total = len(chosen_combos)

        for i, combo in enumerate(chosen_combos, 1):
            if progress_callback:
                try: progress_callback(i, total)
                except Exception: pass

            pred = ' AND '.join(f"{k} = {repr(combo[j])}" for j, k in enumerate(chosen_keys))
            a_part = self.rel.filter(pred)
            b_part = other.rel.filter(pred)
            a_name, b_name = _reg2(self.con, a_part, b_part)
            piece = self.con.sql(join_sql_for(a_name, b_name))

            if rel_out is None:
                rel_out = piece
            else:
                x, y = _reg2(self.con, rel_out, piece)
                rel_out = self.con.sql(f"SELECT * FROM {x} UNION ALL BY NAME SELECT * FROM {y}")

        return Duck(self.con, rel_out if rel_out is not None else self.rel.limit(0))


    def anti_join(
        self,
        other: 'Duck',
        *,
        unnatural_pairs: Optional[Dict[str, str]] = None,
        partition_by: Optional[Sequence[str]] = None,
        max_unions: int = 500,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> 'Duck':
        return self._join_core(
            'ANTI',
            other,
            unnatural_pairs=unnatural_pairs,
            partition_by=partition_by,
            max_unions=max_unions,
            progress_callback=progress_callback,
        )

    # public joins
    def natural_join(
            self,
            other: 'Duck',
            *,
            partition_by: Optional[Sequence[str]] = None,
            max_unions: int = 500,
            progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> 'Duck':
        return self._join_core('NATURAL', other, partition_by=partition_by,
                               max_unions=max_unions, progress_callback=progress_callback)

    def inner_join(
            self,
            other: 'Duck',
            *,
            unnatural_pairs: Optional[Dict[str, str]] = None,
            partition_by: Optional[Sequence[str]] = None,
            max_unions: int = 500,
            progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> 'Duck':
        return self._join_core('INNER', other, unnatural_pairs=unnatural_pairs, partition_by=partition_by,
                               max_unions=max_unions, progress_callback=progress_callback)

    def left_join(
            self,
            other: 'Duck',
            *,
            unnatural_pairs: Optional[Dict[str, str]] = None,
            partition_by: Optional[Sequence[str]] = None,
            max_unions: int = 500,
            progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> 'Duck':
        return self._join_core('LEFT', other, unnatural_pairs=unnatural_pairs, partition_by=partition_by,
                               max_unions=max_unions, progress_callback=progress_callback)

    def semi_join(
            self,
            other: 'Duck',
            *,
            unnatural_pairs: Optional[Dict[str, str]] = None,
            partition_by: Optional[Sequence[str]] = None,
            max_unions: int = 500,
            progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> 'Duck':
        return self._join_core('SEMI', other, unnatural_pairs=unnatural_pairs,
                               partition_by=partition_by, max_unions=max_unions, progress_callback=progress_callback)

    def outer_join(
            self,
            other: 'Duck',
            *,
            unnatural_pairs: Optional[Dict[str, str]] = None,
            partition_by: Optional[Sequence[str]] = None,
            max_unions: int = 500,
            progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> 'Duck':
        return self._join_core('FULL', other, unnatural_pairs=unnatural_pairs, partition_by=partition_by,
                               max_unions=max_unions, progress_callback=progress_callback)

    def cross_join(self, other: 'Duck') -> 'Duck':
        return self._join_core('CROSS', other)

    # ---------- horizontal_stack / vertical_stack / Set ops ----------
    def horizontal_stack(self, other: 'Duck') -> 'Duck':
        """Horizontal stack by row_number alignment; name collisions suffixed with _r."""
        a = _reg1(self.con, self.rel)
        b = _reg1(self.con, other.rel)
        left_cols = ', '.join([f'l.{c} AS {c}' for c in self.columns])
        right_cols_aliased = []
        left_set = set(self.columns)
        for c in other.columns:
            alias = f'{c}_r' if c in left_set else c
            right_cols_aliased.append(f'r.{c} AS {alias}')
        right_cols_sql = ', '.join(right_cols_aliased)
        sql = (
            'WITH L AS (SELECT ROW_NUMBER() OVER() AS rn, * FROM {a}) '
            ', R AS (SELECT ROW_NUMBER() OVER() AS rn, * FROM {b}) '
            'SELECT COALESCE(l.rn, r.rn) AS rn, {left_cols}, {right_cols} '
            'FROM L l FULL JOIN R r ON l.rn = r.rn '
            'ORDER BY COALESCE(l.rn, r.rn)'
        ).format(a=a, b=b, left_cols=left_cols, right_cols=right_cols_sql)
        rel = self.con.sql(sql)
        out = Duck(self.con, rel.select('* EXCLUDE (rn)'))
        out._clear_table_target()
        return out

    def vertical_stack(self, other: 'Duck') -> 'Duck':
        """Vertical stack by name (UNION ALL BY NAME)."""
        a, b = _reg2(self.con, self.rel, other.rel)
        sql = 'SELECT * FROM {a} UNION ALL BY NAME SELECT * FROM {b}'.format(a=a, b=b)
        rel = self.con.sql(sql)
        out = Duck(self.con, rel)
        out._clear_table_target()
        return out

    def union_many(self, *others: 'Duck', by_name: bool = True, distinct: bool = False) -> 'Duck':
        """Chain UNION [ALL] [BY NAME] across many relations."""
        acc = self.rel
        for d in others:
            a, b = _reg2(self.con, acc, d.rel)
            if by_name:
                op = 'UNION ' if distinct else 'UNION ALL '
                sql = 'SELECT * FROM {a} {op}BY NAME SELECT * FROM {b}'.format(a=a, op=op, b=b)
                acc = self.con.sql(sql)
            else:
                op = 'UNION ' if distinct else 'UNION ALL '
                sql = 'SELECT * FROM {a} {op}SELECT * FROM {b}'.format(a=a, op=op, b=b)
                acc = self.con.sql(sql)
        out = Duck(self.con, acc)
        out._clear_table_target()
        return out

    def intersect(self, other: 'Duck', by_name: bool = True) -> 'Duck':
        a, b = _reg2(self.con, self.rel, other.rel)
        if by_name:
            common = [c for c in self.columns if c in other.columns]
            if not common:
                raise KeyError('No common columns to intersect by name.')
            cols = ', '.join(common)
            sql = f'SELECT {cols} FROM {{a}} INTERSECT SELECT {cols} FROM {{b}}'
        else:
            sql = 'SELECT * FROM {a} INTERSECT SELECT * FROM {b}'
        rel = self.con.sql(sql.format(a=a, b=b))
        out = Duck(self.con, rel)
        out._clear_table_target()
        return out

    def except_(self, other: 'Duck', by_name: bool = True) -> 'Duck':
        a, b = _reg2(self.con, self.rel, other.rel)
        if by_name:
            common = [c for c in self.columns if c in other.columns]
            if not common:
                raise KeyError('No common columns to except by name.')
            cols = ', '.join(common)
            sql = f'SELECT {cols} FROM {{a}} EXCEPT SELECT {cols} FROM {{b}}'
        else:
            sql = 'SELECT * FROM {a} EXCEPT SELECT * FROM {b}'
        rel = self.con.sql(sql.format(a=a, b=b))
        out = Duck(self.con, rel)
        out._clear_table_target()
        return out

    # ---------- Window helpers ----------
    def rolling_window(
            self,
            func: Literal['sum', 'avg', 'min', 'max', 'count'],
            partition_by: Sequence[str],
            order_by: str,
            frame_spec: str,
            *,
            target_name: Optional[str] = None,
            on: Optional[str] = None,
    ) -> 'Duck':
        """Append a window aggregation column."""
        if on is None:
            if len(self.columns) != 1:
                raise DuckTypeError('rolling_window requires "on" when relation has != 1 column.')
            on = self.columns[0]
        validate_identifiers(order_by, *(partition_by or []))
        if target_name is None:
            target_name = f'{func}_{on}'
        validate_identifiers(target_name, on)
        part = f'PARTITION BY {", ".join(partition_by)} ' if partition_by else ''
        expr = f'{func}({on}) OVER ({part}ORDER BY {order_by} {frame_spec})'
        return self.add_columns(**{target_name: expr})

    def row_number(self, *, partition_by: Optional[Sequence[str]] = None, order_by: Optional[str] = None,
                   target_name: str = 'row_number') -> 'Duck':
        validate_identifiers(target_name, *(partition_by or []))
        ob = f' ORDER BY {order_by}' if order_by else ''
        part = f'PARTITION BY {", ".join(partition_by)}' if partition_by else ''
        over = f'({part}{ob})' if (part or ob) else '()'
        return self.add_columns(**{target_name: f'row_number() OVER {over}'})

    def percent_rank(self, *, partition_by: Optional[Sequence[str]] = None, order_by: Optional[str] = None,
                     target_name: str = 'percent_rank') -> 'Duck':
        validate_identifiers(target_name, *(partition_by or []))
        ob = f' ORDER BY {order_by}' if order_by else ''
        part = f'PARTITION BY {", ".join(partition_by)}' if partition_by else ''
        over = f'({part}{ob})' if (part or ob) else '()'
        return self.add_columns(**{target_name: f'percent_rank() OVER {over}'})

    def top_k(self, column: str, k: int, *, descending: bool = True, ties: Literal['all', 'first'] = 'all') -> 'Duck':
        """Return top-k rows by a column; ties='all' uses RANK() to include ties; 'first' uses ROW_NUMBER()."""
        validate_identifiers(column)
        order = f'{column} {"DESC" if descending else "ASC"}'
        if ties == 'all':
            ranked = self.add_columns(**{'__rk': f'rank() over (order by {order})'})
            return ranked.filter(f'__rk <= {k}').drop('__rk', skip_missing=True)
        ranked = self.row_number(order_by=order, target_name='__rn')
        limited = ranked.filter(f'__rn <= {k}')
        return limited.drop('__rn', skip_missing=True)

    # ---------- HTML rendering ----------
    def to_html(
            self,
            n: int = 100,
            escape: bool = True,
            *,
            hide: Optional[Sequence[str]] = None,
            categorical: Optional[Dict[str, Dict[Any, str]]] = None,
            all_categorical: Optional[Dict[Any, str]] = None,
            scalar: Optional[Dict[str, Tuple[float, float]]] = None,
            all_scalar: Optional[Tuple[float, float]] = None,
            scalar_palette: Tuple[str, str] = ('#ffffff', '#2196f3'),
            bool_cell: Optional[Dict[str, Tuple[str, str]]] = None,
            row_bool_highlight: Optional[Tuple[str, Tuple[str, str]]] = None,
            caption: Optional[str] = None,
            null_display: str = '',
    ) -> str:
        """HTML table with table-level <style>; supports categorical/boolean classes and per-cell numeric gradients."""
        rows = self.rel.limit(n).fetchall()
        cols_all = self.columns
        hide_cf = {h.casefold() for h in (hide or ())}
        visible: List[Tuple[int, str]] = [(i, c) for i, c in enumerate(cols_all) if c.casefold() not in hide_cf]

        cat = categorical or {}
        all_cat = all_categorical or {}
        scal = scalar or {}
        bool_col_map = bool_cell or {}
        row_hi = row_bool_highlight
        c0, c1 = scalar_palette

        def escape_function(x: Any) -> str:
            s = null_display if x is None else str(x)
            return s if not escape else s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

        css_lines: List[str] = [
            '.duck-table{border-collapse:collapse;font-family:system-ui,Arial,sans-serif;font-size:13px}',
            '.duck-table th,.duck-table td{border:1px solid #ddd;padding:4px 6px;text-align:left}',
            '.duck-table thead{background:#fafafa}',
            '.duck-row-true{ } .duck-row-false{ }',
        ]
        cat_classes: Dict[Tuple[str, Any], str] = {}
        bool_classes: Dict[Tuple[str, bool], str] = {}

        def sanitize(name: str) -> str:
            return re.sub(r'[^A-Za-z0-9_]+', '-', name)

        for col, cmap in cat.items():
            for val, color in cmap.items():
                cls = f'duck-cat-{sanitize(col)}-{sanitize(str(val))}'
                cat_classes[(col, val)] = cls
                css_lines.append(f'.{cls}{{background:{color};}}')
        for val, color in all_cat.items():
            cls = f'duck-cat-ALL-{sanitize(str(val))}'
            cat_classes[('ALL', val)] = cls
            css_lines.append(f'.{cls}{{background:{color};}}')

        for col, (bg_false, bg_true) in bool_col_map.items():
            cls_f = f'duck-bool-{sanitize(col)}-false'
            cls_t = f'duck-bool-{sanitize(col)}-true'
            bool_classes[(col, False)] = cls_f
            bool_classes[(col, True)] = cls_t
            css_lines.extend([f'.{cls_f}{{background:{bg_false};}}', f'.{cls_t}{{background:{bg_true};}}'])

        row_bool_idx: Optional[int] = None
        if row_hi is not None:
            bool_col, (rb_false, rb_true) = row_hi
            resolved, _ = resolve_existing(cols_all, [bool_col], skip_missing=False)
            row_bool_idx = next((i for i, c in enumerate(cols_all) if c == resolved[0]), None)
            css_lines.extend([f'.duck-row-false{{background:{rb_false};}}',
                              f'.duck-row-true{{background:{rb_true};}}'])

        thead = '<thead><tr>' + ''.join(f'<th>{escape_function(col)}</th>' for _, col in visible) + '</tr></thead>'

        def cell_classes(col: str, val: Any) -> List[str]:
            out: List[str] = []
            cmap = cat.get(col)
            if cmap and val in cmap:
                out.append(cat_classes[(col, val)])
            elif not cmap and (str(val) in [str(v) for v in all_cat]): # unhashable type.. list this is not super efficent
                out.append(cat_classes[('ALL', val)])
            if col in bool_col_map and isinstance(val, (bool, type(None))):
                out.append(bool_classes[(col, bool(val))])
            return out

        all_min, all_max = (all_scalar if all_scalar is not None else (None, None))

        def row_html(r: Sequence[Any]) -> str:
            row_open = (f'<tr class="{"duck-row-true" if bool(r[row_bool_idx]) else "duck-row-false"}">'
                        if row_bool_idx is not None else '<tr>')
            tds: List[str] = []
            for idx, col in visible:
                v = r[idx]
                classes = cell_classes(col, v)
                style = ''
                rng = scal.get(col) or (all_min, all_max)
                if isinstance(v, (int, float)) and rng[0] is not None and rng[1] is not None and rng[1] != rng[0]:
                    t = (float(v) - float(rng[0])) / (float(rng[1]) - float(rng[0]))
                    style = f' style="background:{_linear_interpolate_color_color(c0, c1, t)};"'
                cls_attr = (f' class="{" ".join(classes)}"' if classes else '')
                tds.append(f'<td{cls_attr}{style}>{escape_function(v)}</td>')
            return row_open + ''.join(tds) + '</tr>'

        tbody = '<tbody>' + ''.join(row_html(r) for r in rows) + '</tbody>'
        style_tag = '<style>\n' + '\n'.join(css_lines) + '\n</style>'
        cap = f'<caption>{escape_function(caption)}</caption>' if caption else ''
        return style_tag + '\n<table class="duck-table">\n' + cap + '\n' + thead + '\n' + tbody + '\n</table>'

    # ---------- materialize / explain ----------
    def materialize(
        self,
        name: Optional[str] = None,
        *,
        temp: bool = True,
        passthrough: bool = False,
        rowcount_dict: Optional[dict[str, int]] = None,
        on_metric: Optional[Callable[[dict], None]] = None,
        tag: Optional[str] = None,
    ) -> 'Duck':
        """Materialize into a TEMP TABLE (not a view). If passthrough=True, return self."""
        t0 = time.perf_counter()

        if passthrough:
            # Optional: emit a 'skip' metric so you can see it in logs
            if on_metric:
                on_metric({
                    "event": "materialize_skip",
                    "tag": tag or "",
                    "temp": temp,
                    "table": None,
                    "rows": None,
                    "ms": 0.0,
                })
            return self

        table_name = name or f'tmp_{uuid.uuid4().hex}'
        validate_identifiers(table_name)
        a = _reg1(self.con, self.rel)

        if temp:
            self.con.execute('CREATE TEMP TABLE {t} AS SELECT * FROM {a}'.format(t=table_name, a=a))
        else:
            self.con.execute('CREATE TABLE {t} AS SELECT * FROM {a}'.format(t=table_name, a=a))

        table = Duck.table(self.con, table_name)

        # Only count when requested; COUNT(*) can be expensive.
        rows = None
        if rowcount_dict is not None or on_metric:
            rows = table.count_rows()

        if rowcount_dict is not None:
            for k in list(rowcount_dict.keys()):
                rowcount_dict[k] = int(rows or 0)

        if on_metric:
            on_metric({
                "event": "materialize",
                "tag": tag or "",
                "temp": temp,
                "table": table_name,
                "rows": int(rows or 0),
                "ms": (time.perf_counter() - t0) * 1000.0,
            })

        return table

    def materialize_cache(
            self,
            path_fn: Callable[['Duck'], Path],
            *,
            compression: Literal['ZSTD', 'SNAPPY', 'GZIP', 'LZ4', 'BROTLI', 'UNCOMPRESSED'] = 'ZSTD',
            schema_check: bool = True,
    ) -> 'Duck':
        """
        Cache materialization to Parquet using a user-provided path function.
        """
        target = path_fn(self)
        if target.exists():
            cached = Duck.from_parquet(self.con, target)
            if schema_check:
                cur_cols = self.columns
                cache_cols = cached.columns
                if cur_cols != cache_cols:
                    raise DuckIOError('Cached schema mismatch.')
            return cached
        self.write_parquet(target, compression=compression)
        return Duck.from_parquet(self.con, target)

    def explain(self, *, explain_type: Literal['text', 'json'] = 'text') -> str:
        """Return DuckDB plan; 'json' when engine supports it, else falls back to text."""
        a = _reg1(self.con, self.rel)
        if explain_type == 'text':
            sql = 'EXPLAIN SELECT * FROM {a}'.format(a=a)
            rel = self.con.sql(sql)
            return '\n'.join(r[0] for r in rel.fetchall())
        try:
            sql = 'EXPLAIN (FORMAT JSON) SELECT * FROM {a}'.format(a=a)
            rel = self.con.sql(sql)
            return '\n'.join(r[0] for r in rel.fetchall())
        except duckdb.Error:
            sql = 'EXPLAIN SELECT * FROM {a}'.format(a=a)
            rel = self.con.sql(sql)
            return '\n'.join(r[0] for r in rel.fetchall())

    def explain_analyze(self, *, explain_format: Literal['text', 'json'] = 'text') -> str:
        """Return DuckDB analyze plan."""
        a = _reg1(self.con, self.rel)
        if explain_format == 'text':
            sql = 'EXPLAIN ANALYZE SELECT * FROM {a}'.format(a=a)
            rel = self.con.sql(sql)
            return '\n'.join(r[0] for r in rel.fetchall())
        try:
            sql = 'EXPLAIN ANALYZE (FORMAT JSON) SELECT * FROM {a}'.format(a=a)
            rel = self.con.sql(sql)
            return '\n'.join(r[0] for r in rel.fetchall())
        except duckdb.Error:
            sql = 'EXPLAIN ANALYZE SELECT * FROM {a}'.format(a=a)
            rel = self.con.sql(sql)
            return '\n'.join(r[0] for r in rel.fetchall())


    def select_columns(self, *columns: str, missing_ok=True) -> 'Duck':
        known_columns = [col.lower() for col in self.columns]
        missing = [col for col in columns if col.lower() not in known_columns]
        if missing and not missing_ok:
            raise ValueError(f'Missing columns {tuple(missing)} in select statement\n try any of {tuple(known_columns)}')
        columns_to_select = [col for col in columns if col.lower() not in missing] if missing_ok else list(columns)
        if not columns_to_select:
            return self
        projection = ", ".join(columns_to_select)   # <-- build a single expression
        try:
            out = Duck(self.con, self.rel.select(projection))
        except duckdb.ParserException as e:
            try:
                e.add_note(str(tuple(columns_to_select)))
            except Exception:
                pass
            raise e
        out._clear_table_target()
        return out


    def split(
        self,
        predicate_sql: str,
        *,
        include_nulls_in_else: bool = True,
    ) -> Tuple['Duck', 'Duck']:
        """
        Split the relation into two Ducks based on a predicate (like .filter).
        Returns (matched, else_bucket).

        - matched: rows where predicate is TRUE
        - else_bucket: rows where predicate is FALSE
          + if include_nulls_in_else=True (default), also includes rows where predicate evaluates to NULL
        """
        a = _reg1(self.con, self.rel)

        # TRUE branch
        rel_true = self.con.sql(f"SELECT * FROM {a} WHERE {predicate_sql}")

        # ELSE branch (FALSE or optionally NULL)
        else_clause = (
            f"NOT ({predicate_sql}) OR ({predicate_sql}) IS NULL"
            if include_nulls_in_else
            else f"NOT ({predicate_sql})"
        )
        rel_else = self.con.sql(f"SELECT * FROM {a} WHERE {else_clause}")

        d_true = Duck(self.con, rel_true);  d_true._clear_table_target()
        d_else = Duck(self.con, rel_else);  d_else._clear_table_target()
        return d_true, d_else

    
    # ---------- pass-through select ----------
    def select(self, projection: str) -> 'Duck':
        try:
            out = Duck(self.con, self.rel.select(projection))
        except duckdb.ParserException as e:
            e.add_note(projection)
            raise e
        out._clear_table_target()
        return out

    # ---------- Pivot / Unpivot (thin wrappers) ----------
    def pivot(self, on: str, values: str, agg: str, *, group_by: Optional[Sequence[str]] = None) -> 'Duck':
        """Thin wrapper for DuckDB PIVOT with dynamic IN list."""
        a = _reg1(self.con, self.rel)
        gb = f' GROUP BY {", ".join(group_by)}' if group_by else ''

        sql = 'SELECT * FROM {a} PIVOT({agg}({values}) FOR {on}){gb}'.format(
            a=a, agg=agg, values=values, on=on, gb=gb
        )

        try:
            rel = self.con.sql(sql)
        except duckdb.ParserException as e:
            e.add_note(sql)
            raise e
        out = Duck(self.con, rel)
        out._clear_table_target()
        return out

    def unpivot(
            self,
            columns: Optional[Sequence[str]] = None,
            into_name: str = 'key',
            value_name: str = 'value',
            *,
            exclude: Optional[Sequence[str]] = None,
    ) -> 'Duck':
        """Thin wrapper for DuckDB UNPIVOT with optional exclude support."""
        validate_identifiers(into_name, value_name, *(columns or []), *(exclude or []))
        if columns and exclude:
            raise DuckTypeError('Provide either columns or exclude, not both.')
        a = _reg1(self.con, self.rel)
        if exclude:
            ex_cf = {e.casefold() for e in exclude}
            cols = [c for c in self.columns if c.casefold() not in ex_cf]
        else:
            cols = list(columns or self.columns)
        cols_sql = ', '.join(cols) if cols else '*'
        sql = 'SELECT * FROM {a} UNPIVOT({cols} INTO {k} VALUES {v})'.format(
            a=a, cols=cols_sql, k=into_name, v=value_name
        )
        rel = self.con.sql(sql)
        out = Duck(self.con, rel)
        out._clear_table_target()
        return out
    # ---------- Column name normalization ----------
    @staticmethod
    def _strip_matching_quotes(s: str) -> str:
        """Remove one pair of matching leading/trailing quotes (' " `) if present."""
        if not s:
            return s
        q = s[0]
        if q in ("'", '"', '`') and len(s) >= 2 and s[-1] == q:
            return s[1:-1]
        return s

    @staticmethod
    def normalize_column_name(name: str) -> str:
        """
        Normalize to snake_case, remove quotes/backticks, and ensure a safe leading char.
        Rules:
          - Trim whitespace
          - Strip one pair of matching leading/trailing quotes (' " `) if present
          - Remove any remaining quote/backtick characters inside
          - Replace any run of non-alphanumeric chars with a single underscore
          - Lowercase
          - Collapse multiple underscores and strip leading/trailing underscores
          - If first char is not [a-z_] prepend underscore
          - Never return empty; fall back to "_"
        """
        if name is None:
            return "_"
        s = str(name).strip()
        s = Duck._strip_matching_quotes(s)
        # Remove any leftover quotes/backticks inside
        s = s.replace('"', ' ').replace("'", ' ').replace('`', ' ')
        # Non-alphanumeric -> underscore
        s = re.sub(r'[^A-Za-z0-9]+', '_', s)
        s = s.lower()
        # Collapse and trim underscores
        s = re.sub(r'_+', '_', s).strip('_')
        if not s:
            return "_"
        # Safe leading char
        first = s[0]
        if not (('a' <= first <= 'z') or first == '_'):
            s = '_' + s
        return s

    def normalize_columns(self, ensure_unique: bool = True) -> 'Duck':
        """
        Return a new Duck with all columns normalized via normalize_column_name().
        If ensure_unique=True, append _2, _3, ... to resolve collisions.
        """
        orig = list(self.columns)
        normalized: Dict[str, int] = {}
        rename_map: Dict[str, str] = {}

        for c in orig:
            base = Duck.normalize_column_name(c)
            count = normalized.get(base, 0) + 1
            normalized[base] = count
            new_name = base if count == 1 else base + '_' + str(count)
            rename_map[new_name] = c  # new -> old (Duck.rename expects new_to_old)

        # If nothing changes, return self
        unchanged = True
        i = 0
        for c in orig:
            i += 1
            base = Duck.normalize_column_name(c)
            idx = 1
            if ensure_unique and normalized.get(base, 0) > 1:
                # compute expected unique name for this occurrence
                seen = 0
                for j in range(i):
                    if Duck.normalize_column_name(orig[j]) == base:
                        seen += 1
                expected_new = base if seen == 1 else base + '_' + str(seen)
            else:
                expected_new = base
            if expected_new != c:
                unchanged = False
                break

        if unchanged:
            return self

        return self.rename(**rename_map)

    def append_csv(
        self,
        csv_path: Path | str,
        *,
        encoding: Encoding = "utf-8",
        create_parents: bool = True,
        include_header_if_new: bool = True,
    ) -> int:
        """
        Append the current relation's rows to a CSV file using Python's csv module.

        Behavior
        --------
        - Creates parent directories when `create_parents=True`.
        - Writes a header row IFF the file does not exist or is empty AND
        `include_header_if_new=True`.
        - Uses the relation's column order as CSV fieldnames.
        - Returns the number of data rows written (not counting header).

        Parameters
        ----------
        csv_path : Path | str
            Destination CSV file (appended to).
        encoding : Encoding, default 'utf-8'
            File encoding used when opening the CSV.
        create_parents : bool, default True
            If True, ensure parent directories exist.
        include_header_if_new : bool, default True
            If True, write header when file is new/empty.

        Returns
        -------
        int
            Count of rows appended.
        """
        # Gather rows and columns
        cols = list(self.columns)
        if not cols:
            return 0  # nothing to write
        rows = self.rel.fetchall()
        if not rows:
            return 0

        # Prepare path
        p = Path(csv_path) if not isinstance(csv_path, Path) else csv_path
        if create_parents:
            p.parent.mkdir(parents=True, exist_ok=True)

        # Determine if we should write header
        needs_header = False
        if include_header_if_new:
            try:
                needs_header = (not p.exists()) or (p.stat().st_size == 0)
            except OSError:
                needs_header = True  # if stat fails, try to write header

        # Append rows
        try:
            with p.open(mode="a", newline="", encoding=encoding) as f:
                writer = csv.DictWriter(f, fieldnames=cols)
                if needs_header:
                    writer.writeheader()
                for r in rows:
                    writer.writerow({cols[i]: r[i] for i in range(len(cols))})
            return len(rows)
        except Exception as e:
            raise DuckIOError(f"append_csv failed for {p!s}: {e}") from e

    # ---------- Debug config (opt-in; not set in __init__) ----------
    def start_debug(
        self,
        *,
        print_operation_duration: bool = True,
        print_columns_after_operation: bool = False,
        duration_threshold_ms: int = 0,
        **extras: Any,
    ) -> 'Duck':
        """
        Enable lightweight, inheritable debug instrumentation on this Duck instance.

        Parameters
        ----------
        print_operation_duration : bool, default True
            If True, prints how long each decorated method took.
        print_columns_after_operation : bool, default False
            If True (and the method returns a Duck), prints the column names of the result.
        **extras : Any
            Future/debug-specific flags you may want to add later; stored but ignored by the decorator
            unless explicitly handled.

        Notes
        -----
        - Debug settings *inherit* onto any Duck returned by decorated methods.
        - Call .end_debug() on an instance to stop inheriting/printing from that instance onward.
        """
        cfg = dict(extras)
        cfg['print_operation_duration'] = bool(print_operation_duration)
        cfg['print_columns_after_operation'] = bool(print_columns_after_operation)
        cfg['duration_threshold_ms'] = int(duration_threshold_ms)
        setattr(self, '_debug_config', cfg)
        return self

    def end_debug(self) -> 'Duck':
        """
        Disable debug instrumentation for this Duck instance (does not retroactively affect
        previously-returned Ducks that already inherited a config).
        """
        if hasattr(self, '_debug_config'):
            delattr(self, '_debug_config')
        return self


def _duck_debug_instrument(func):
    """
    Method decorator applied dynamically to Duck instance methods.
    It is a no-op unless `self._debug_config` is present (set via .start_debug()).
    """
    @wraps(func)
    def _wrapped(self, *args, **kwargs):
        cfg = getattr(self, '_debug_config', None)
        if not cfg:
            return func(self, *args, **kwargs)

        # Resolve callsite (file:line) of the *caller* of this method
        caller_file, caller_line = "<unknown>", 0
        if cfg.get('print_operation_duration', False):
            try:
                import sys, os
                from pathlib import Path
                f = sys._getframe(1)  # caller of _wrapped
                this_file = os.path.abspath(__file__)
                # Skip frames from this module (chained internal calls)
                while f and os.path.abspath(f.f_code.co_filename) == this_file:
                    f = f.f_back
                if f:
                    caller_file = Path(f.f_code.co_filename).name
                    caller_line = f.f_lineno
                del f  # avoid reference cycles
            except Exception:
                pass

        t0 = time.perf_counter()
        try:
            result = func(self, *args, **kwargs)
        except Exception as e:
            t1 = time.perf_counter()
            if cfg.get('print_operation_duration', False):
                duration_ms = (t1 - t0) * 1000.0
                print(
                    f"[Duck.debug] {func.__name__} raised {e.__class__.__name__} "
                    f"after {duration_ms:.2f} ms @ {caller_file}:{caller_line}"
                )
            raise

        t1 = time.perf_counter()
        if cfg.get('print_operation_duration', False):
            duration_ms = (t1 - t0) * 1000.0
            if duration_ms > float(cfg.get('duration_threshold_ms', 0)):
                print(
                    f"[Duck.debug] {func.__name__} took {duration_ms:.2f} ms "
                    f"@ {caller_file}:{caller_line}"
                )

        # Inherit debug config onto Duck results & optionally print columns
        try:
            if isinstance(result, Duck):
                result._debug_config = dict(cfg)
                if cfg.get('print_columns_after_operation', False):
                    try:
                        print(f"[Duck.debug] {func.__name__} columns: {tuple(result.columns)}")
                    except Exception as col_err:
                        print(f"[Duck.debug] {func.__name__} columns: <error {col_err!r}>")
        except NameError:
            pass

        return result
    return _wrapped


def _apply_duck_debug_wrappers():
    """
    Dynamically wrap public instance methods of Duck with debug instrumentation.
    - Skips dunder methods, properties, classmethods, staticmethods, and the debug setters themselves.
    """
    for name, attr in list(Duck.__dict__.items()):
        # skip dunders & debug control methods
        if name.startswith('__') or name in ('start_debug', 'end_debug'):
            continue
        # skip descriptors not plain instance methods
        if isinstance(attr, (property, classmethod, staticmethod)):
            continue
        # only wrap plain functions that will become bound instance methods
        if inspect.isfunction(attr):
            setattr(Duck, name, _duck_debug_instrument(attr))

# Apply wrappers once at import time
_apply_duck_debug_wrappers()
