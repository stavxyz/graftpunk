"""Hacker News plugin - NoDriver backend example.

This plugin demonstrates:
- Form-based login with NoDriver (async)
- Real-world site integration
- Better anti-detection for modern sites

Site: https://news.ycombinator.com
Auth: Requires real Hacker News account

Usage:
    1. Symlink to plugins directory:
       ln -s $(pwd)/examples/plugins/hackernews.py ~/.config/graftpunk/plugins/

    2. Log in (opens browser, requires real credentials):
       python -c "
       import asyncio
       from graftpunk.plugins.python_loader import discover_python_plugins
       plugins = discover_python_plugins().plugins
       plugin = next(p for p in plugins if p.site_name == 'hn')
       asyncio.run(plugin.login('your_username', 'your_password'))
       "

    3. Use cached session:
       gp hn front
       gp hn front --page 2
       gp hn saved  # requires login
"""

import asyncio

from graftpunk import BrowserSession, cache_session
from graftpunk.plugins import SitePlugin, command


class HackerNewsPlugin(SitePlugin):
    """Plugin for Hacker News (news.ycombinator.com)."""

    site_name = "hn"
    session_name = "hackernews"
    help_text = "Hacker News commands"

    base_url = "https://news.ycombinator.com"

    async def login(self, username: str, password: str) -> bool:
        """Log in to Hacker News and cache the session.

        Uses NoDriver backend for better anti-detection.

        Args:
            username: Your Hacker News username.
            password: Your Hacker News password.

        Returns:
            True if login successful.
        """
        session = BrowserSession(backend="nodriver", headless=False)

        # NoDriver is async
        await session.driver.get(f"{self.base_url}/login")

        # Find and fill login form
        acct_field = await session.driver.select("input[name='acct']")
        await acct_field.send_keys(username)

        pw_field = await session.driver.select("input[name='pw']")
        await pw_field.send_keys(password)

        submit = await session.driver.select("input[value='login']")
        await submit.click()

        # Wait for login to complete (logout link appears)
        # Give it time since HN can be slow
        await asyncio.sleep(2)

        # Cache the session
        cache_session(session, self.session_name)
        await session.quit()
        return True

    @command(help="Get front page stories")
    def front(self, session, page: int = 1):
        """Fetch front page stories.

        Args:
            session: Authenticated requests session.
            page: Page number (default: 1).

        Returns:
            Dict with URL and status code.
        """
        response = session.get(f"{self.base_url}/news?p={page}")
        return {
            "url": response.url,
            "status": response.status_code,
            "page": page,
        }

    @command(help="Get newest stories")
    def newest(self, session, page: int = 1):
        """Fetch newest stories.

        Args:
            session: Authenticated requests session.
            page: Page number (default: 1).

        Returns:
            Dict with URL and status code.
        """
        response = session.get(f"{self.base_url}/newest?p={page}")
        return {
            "url": response.url,
            "status": response.status_code,
            "page": page,
        }

    @command(help="Get saved stories (requires login)")
    def saved(self, session):
        """Fetch your saved stories.

        Requires an authenticated session.

        Args:
            session: Authenticated requests session.

        Returns:
            Dict with URL and status code.
        """
        response = session.get(f"{self.base_url}/saved")
        return {
            "url": response.url,
            "status": response.status_code,
        }
