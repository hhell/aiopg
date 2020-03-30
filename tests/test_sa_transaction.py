import asyncio
from unittest import mock

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, func, select

from aiopg import cursor, sa
from aiopg.sa.exc import InvalidRequestError

meta = MetaData()
tbl = Table('sa_tbl2', meta,
            Column('id', Integer, nullable=False,
                   primary_key=True),
            Column('name', String(255)))


@pytest.fixture
def connect(make_connection):
    async def go(**kwargs):
        conn = await make_connection(**kwargs)
        cur = await conn.cursor()
        await cur.execute("DROP TABLE IF EXISTS sa_tbl2")
        await cur.execute("CREATE TABLE sa_tbl2 "
                          "(id serial, name varchar(255))")
        await cur.execute("INSERT INTO sa_tbl2 (name)"
                          "VALUES ('first')")
        cur.close()

        engine = mock.Mock(from_spec=sa.engine.Engine)
        engine.dialect = sa.engine._dialect
        return sa.SAConnection(conn, engine)

    yield go


@pytest.fixture
def xa_connect(connect):
    async def go(**kwargs):
        conn = await connect(**kwargs)
        val = await conn.scalar('show max_prepared_transactions')
        if not int(val):
            raise pytest.skip('Twophase transacions are not supported. '
                              'Set max_prepared_transactions to '
                              'a nonzero value')
        return conn

    yield go


async def test_without_transactions(connect):
    conn1 = await connect()
    conn2 = await connect()
    res1 = await conn1.scalar(select([func.count()]).select_from(tbl))
    assert 1 == res1

    await conn2.execute(tbl.delete())

    res2 = await conn1.scalar(select([func.count()]).select_from(tbl))
    assert 0 == res2


async def test_connection_attr(connect):
    conn = await connect()
    tr = await conn.begin()
    assert tr.connection is conn


async def test_root_transaction(connect):
    conn1 = await connect()
    conn2 = await connect()

    tr = await conn1.begin()
    assert tr.is_active
    await conn1.execute(tbl.delete())

    res1 = await conn2.scalar(select([func.count()]).select_from(tbl))
    assert 1 == res1

    await tr.commit()

    assert not tr.is_active
    assert not conn1.in_transaction
    res2 = await conn2.scalar(select([func.count()]).select_from(tbl))
    assert 0 == res2


async def test_root_transaction_rollback(connect):
    conn1 = await connect()
    conn2 = await connect()

    tr = await conn1.begin()
    assert tr.is_active
    await conn1.execute(tbl.delete())

    res1 = await conn2.scalar(select([func.count()]).select_from(tbl))
    assert 1 == res1

    await tr.rollback()

    assert not tr.is_active
    res2 = await conn2.scalar(select([func.count()]).select_from(tbl))
    assert 1 == res2


async def test_root_transaction_close(connect):
    conn1 = await connect()
    conn2 = await connect()

    tr = await conn1.begin()
    assert tr.is_active
    await conn1.execute(tbl.delete())

    res1 = await conn2.scalar(select([func.count()]).select_from(tbl))
    assert 1 == res1

    await tr.close()

    assert not tr.is_active
    res2 = await conn2.scalar(select([func.count()]).select_from(tbl))
    assert 1 == res2


async def test_root_transaction_commit_inactive(connect):
    conn = await connect()
    tr = await conn.begin()
    assert tr.is_active
    await tr.commit()
    assert not tr.is_active
    with pytest.raises(sa.InvalidRequestError):
        await tr.commit()


async def test_root_transaction_rollback_inactive(connect):
    conn = await connect()
    tr = await conn.begin()
    assert tr.is_active
    await tr.rollback()
    assert not tr.is_active
    await tr.rollback()
    assert not tr.is_active


async def test_root_transaction_double_close(connect):
    conn = await connect()
    tr = await conn.begin()
    assert tr.is_active
    await tr.close()
    assert not tr.is_active
    await tr.close()
    assert not tr.is_active


async def test_inner_transaction_commit(connect):
    conn = await connect()
    tr1 = await conn.begin()
    tr2 = await conn.begin()
    assert tr2.is_active

    await tr2.commit()
    assert not tr2.is_active
    assert tr1.is_active

    await tr1.commit()
    assert not tr2.is_active
    assert not tr1.is_active


async def test_rollback_on_connection_close(connect):
    conn1 = await connect()
    conn2 = await connect()

    tr = await conn1.begin()
    await conn1.execute(tbl.delete())

    res1 = await conn2.scalar(select([func.count()]).select_from(tbl))
    assert 1 == res1

    await conn1.close()

    res2 = await conn2.scalar(select([func.count()]).select_from(tbl))
    assert 1 == res2
    del tr


async def test_inner_transaction_rollback(connect):
    conn = await connect()
    tr1 = await conn.begin()
    tr2 = await conn.begin()
    assert tr2.is_active
    await conn.execute(tbl.insert().values(name='aaaa'))

    await tr2.rollback()
    assert not tr2.is_active
    assert not tr1.is_active

    res = await conn.scalar(select([func.count()]).select_from(tbl))
    assert 1 == res


async def test_inner_transaction_close(connect):
    conn = await connect()
    tr1 = await conn.begin()
    tr2 = await conn.begin()
    assert tr2.is_active
    await conn.execute(tbl.insert().values(name='aaaa'))

    await tr2.close()
    assert not tr2.is_active
    assert tr1.is_active
    await tr1.commit()

    res = await conn.scalar(select([func.count()]).select_from(tbl))
    assert 2 == res


async def test_nested_transaction_commit(connect):
    conn = await connect()
    tr1 = await conn.begin_nested()
    tr2 = await conn.begin_nested()
    assert tr1.is_active
    assert tr2.is_active

    await conn.execute(tbl.insert().values(name='aaaa'))
    await tr2.commit()
    assert not tr2.is_active
    assert tr1.is_active

    res = await conn.scalar(select([func.count()]).select_from(tbl))
    assert 2 == res

    await tr1.commit()
    assert not tr2.is_active
    assert not tr1.is_active

    res = await conn.scalar(select([func.count()]).select_from(tbl))
    assert 2 == res


async def test_nested_transaction_commit_twice(connect):
    conn = await connect()
    tr1 = await conn.begin_nested()
    tr2 = await conn.begin_nested()

    await conn.execute(tbl.insert().values(name='aaaa'))
    await tr2.commit()
    assert not tr2.is_active
    assert tr1.is_active

    await tr2.commit()
    assert not tr2.is_active
    assert tr1.is_active

    res = await conn.scalar(select([func.count()]).select_from(tbl))
    assert 2 == res

    await tr1.close()


async def test_nested_transaction_rollback(connect):
    conn = await connect()
    tr1 = await conn.begin_nested()
    tr2 = await conn.begin_nested()
    assert tr1.is_active
    assert tr2.is_active

    await conn.execute(tbl.insert().values(name='aaaa'))
    await tr2.rollback()
    assert not tr2.is_active
    assert tr1.is_active

    res = await conn.scalar(select([func.count()]).select_from(tbl))
    assert 1 == res

    await tr1.commit()
    assert not tr2.is_active
    assert not tr1.is_active

    res = await conn.scalar(select([func.count()]).select_from(tbl))
    assert 1 == res


async def test_nested_transaction_rollback_twice(connect):
    conn = await connect()
    tr1 = await conn.begin_nested()
    tr2 = await conn.begin_nested()

    await conn.execute(tbl.insert().values(name='aaaa'))
    await tr2.rollback()
    assert not tr2.is_active
    assert tr1.is_active

    await tr2.rollback()
    assert not tr2.is_active
    assert tr1.is_active

    await tr1.commit()
    res = await conn.scalar(select([func.count()]).select_from(tbl))
    assert 1 == res


async def test_twophase_transaction_commit(xa_connect):
    conn = await xa_connect()
    tr = await conn.begin_twophase()
    await conn.execute(tbl.insert().values(name='aaaa'))

    await tr.prepare()
    assert tr.is_active

    await tr.commit()
    assert not tr.is_active

    res = await conn.scalar(select([func.count()]).select_from(tbl))
    assert 2 == res


async def test_twophase_transaction_twice(xa_connect):
    conn = await xa_connect()
    tr = await conn.begin_twophase()
    with pytest.raises(sa.InvalidRequestError):
        await conn.begin_twophase()

    assert tr.is_active
    await tr.prepare()
    await tr.commit()


async def test_transactions_sequence(xa_connect):
    conn = await xa_connect()

    await conn.execute(tbl.delete())

    assert conn._transaction is None

    tr1 = await conn.begin()
    assert tr1 is conn._transaction
    await conn.execute(tbl.insert().values(name='a'))
    res1 = await conn.scalar(select([func.count()]).select_from(tbl))
    assert 1 == res1

    await tr1.commit()
    assert conn._transaction is None

    tr2 = await conn.begin()
    assert tr2 is conn._transaction
    await conn.execute(tbl.insert().values(name='b'))
    res2 = await conn.scalar(select([func.count()]).select_from(tbl))
    assert 2 == res2

    await tr2.rollback()
    assert conn._transaction is None

    tr3 = await conn.begin()
    assert tr3 is conn._transaction
    await conn.execute(tbl.insert().values(name='b'))
    res3 = await conn.scalar(select([func.count()]).select_from(tbl))
    assert 2 == res3

    await tr3.commit()
    assert conn._transaction is None


async def test_transaction_mode(connect):
    conn = await connect()

    await conn.execute(tbl.delete())

    tr1 = await conn.begin(isolation_level='SERIALIZABLE')
    await conn.execute(tbl.insert().values(name='a'))
    res1 = await conn.scalar(select([func.count()]).select_from(tbl))
    assert 1 == res1
    await tr1.commit()

    tr2 = await conn.begin(isolation_level='REPEATABLE READ')
    await conn.execute(tbl.insert().values(name='b'))
    res2 = await conn.scalar(select([func.count()]).select_from(tbl))
    assert 2 == res2
    await tr2.commit()

    tr3 = await conn.begin(isolation_level='READ UNCOMMITTED')
    await conn.execute(tbl.insert().values(name='c'))
    res3 = await conn.scalar(select([func.count()]).select_from(tbl))
    assert 3 == res3
    await tr3.commit()

    tr4 = await conn.begin(readonly=True)
    assert tr4 is conn._transaction
    res1 = await conn.scalar(select([func.count()]).select_from(tbl))
    assert 3 == res1
    await tr4.commit()

    tr5 = await conn.begin(isolation_level='READ UNCOMMITTED',
                           readonly=True)
    res1 = await conn.scalar(select([func.count()]).select_from(tbl))
    assert 3 == res1
    await tr5.commit()

    tr6 = await conn.begin(deferrable=True)
    await conn.execute(tbl.insert().values(name='f'))
    res1 = await conn.scalar(select([func.count()]).select_from(tbl))
    assert 4 == res1
    await tr6.commit()

    tr7 = await conn.begin(isolation_level='REPEATABLE READ',
                           deferrable=True)
    await conn.execute(tbl.insert().values(name='g'))
    res1 = await conn.scalar(select([func.count()]).select_from(tbl))
    assert 5 == res1
    await tr7.commit()

    tr8 = await conn.begin(isolation_level='SERIALIZABLE',
                           readonly=True, deferrable=True)
    assert tr8 is conn._transaction
    res1 = await conn.scalar(select([func.count()]).select_from(tbl))
    assert 5 == res1
    await tr8.commit()


async def test_transaction_error_on_begin(pg_params, monkeypatch):

    execute_orig = cursor.Cursor.execute

    class InternalTestException(Exception):
        """A very special exception"""

    async def execute_mockup(self, operation, *args, **kwargs):
        if operation == 'BEGIN':
            raise InternalTestException("Simulating error-at-tx-begin")
        return await execute_orig(self, operation, *args, **kwargs)

    monkeypatch.setattr(cursor.Cursor, 'execute', execute_mockup)

    params = pg_params.copy()
    params.update(minsize=2, maxsize=5, timeout=6)

    engine = await sa.create_engine(**params)

    async def one_go():
        with pytest.raises(InvalidRequestError):
            async with pool.acquire() as conn:
                async with conn.begin() as tx:
                    assert tx
                    await conn.execute("SELECT 1")

    async with engine as pool:
        for idx in range(50):
            # TODO: this doesn't properly time out when it fails
            # (likely waiting forever for cancellation).
            await asyncio.wait_for(one_go(), timeout=10)
