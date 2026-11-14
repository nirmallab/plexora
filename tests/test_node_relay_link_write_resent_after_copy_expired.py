"""A write the page sends again after the node's copy expired is not run twice.

The page (services/relayRoom.js) keeps every unfinished request, writes
included, for as long as it is away, and sends each again under its own
stream id once the node's HELLO says both ends are back: "nothing is done
twice". The node (plexora/server/node/relay_link.py) keeps an answered write's
copy for `cache_ttl` (120 s), and a paused one for as long. A page away for
longer -- a closed laptop, a Wi-Fi change -- must still not have its POST
executed a second time on the node's server, whether the first run had
finished while the page was away or was still running.

Uses the relay, page and origin stand-ins of tests/test_node_relay_link.py,
with a copy that lives 0.3 s instead of two minutes.
"""

import asyncio

from tests.test_node_relay_link import Rig, origin, run, until, wait_event  # noqa: F401


def test_a_post_answered_while_the_page_was_away_past_the_copys_life_is_not_run_again(origin):
    """The page drops mid-POST and the node's own socket stays up: the node
    finishes, its copy expires, and the page's resend must not execute the
    POST a second time."""
    async def scenario():
        async with Rig(origin, cache_ttl=0.3) as rig:
            page = await rig.joined_page()
            stream, future = page.start("POST", "/gate/a", body=b"payload")
            await page.send(stream)
            await wait_event(origin.entered_event("a"))
            await page.drop()
            origin.gate("a").set()
            key = (page.client, stream)
            await until(lambda: key in rig.link._exchanges and rig.link._exchanges[key].done,
                        what="the node to finish the POST")
            await until(lambda: key not in rig.link._exchanges,
                        what="the node's copy to expire")
            # Back after the copy's life, with a new pid: the same stream again.
            await page.reconnect()
            answer = await asyncio.wait_for(future, 10)
            assert origin.counts["/gate/a"] == 1, (
                f"the POST ran {origin.counts['/gate/a']} times; the resend answered "
                f"{answer['status']} {answer['body']!r}")

    run(scenario())


def test_a_post_still_running_when_page_and_node_were_away_past_the_copys_life_is_not_run_again(origin):
    """The page drops mid-POST, then the node's socket too: the paused
    exchange outlives `cache_ttl` while the node's server is still executing
    it, and the page's resend must not start a second, concurrent run."""
    async def scenario():
        async with Rig(origin, cache_ttl=0.3) as rig:
            page = await rig.joined_page()
            stream, future = page.start("POST", "/gate/b", body=b"payload")
            await page.send(stream)
            await wait_event(origin.entered_event("b"))
            await page.drop()
            await rig.relay.drop_node(rig.room)
            key = (page.client, stream)
            await until(lambda: key not in rig.link._exchanges,
                        what="the node to give up on the paused POST")
            await until(lambda: rig.link.connected, what="the node's new socket")
            await page.reconnect()
            # Give the resend time to reach the node's server while the first
            # run is still inside it.
            await until(lambda: origin.counts["/gate/b"] >= 2 or key in rig.link._exchanges,
                        what="the node to take the resend")
            await asyncio.sleep(0.3)
            running_together = origin.counts["/gate/b"]
            origin.gate("b").set()
            answer = await asyncio.wait_for(future, 10)
            assert running_together == 1, (
                f"{running_together} runs of the POST were inside the node's server at once")
            assert origin.counts["/gate/b"] == 1, (
                f"the POST ran {origin.counts['/gate/b']} times; the resend answered "
                f"{answer['status']} {answer['body']!r}")

    run(scenario())
