"""Quotes to Scrape plugin - Selenium backend example.

This plugin demonstrates:
- Form-based login with Selenium
- Caching sessions for later API use
- The @command decorator for CLI commands

Site: https://quotes.toscrape.com
Auth: Any username/password works (it's a test site)

Usage:
    1. Symlink to plugins directory:
       ln -s $(pwd)/examples/plugins/quotes.py ~/.config/graftpunk/plugins/

    2. Log in (opens browser):
       python -c "
       from graftpunk.plugins.python_loader import discover_python_plugins
       plugins = discover_python_plugins().plugins
       plugin = next(p for p in plugins if p.site_name == 'quotes')
       plugin.login()
       "

    3. Use cached session:
       gp quotes list
       gp quotes list --page 2
"""

from graftpunk import BrowserSession, cache_session
from graftpunk.plugins import SitePlugin, command


class QuotesPlugin(SitePlugin):
    """Plugin for quotes.toscrape.com (test site, any credentials work)."""

    site_name = "quotes"
    session_name = "quotes"
    help_text = "Quotes to Scrape commands (test site)"

    base_url = "https://quotes.toscrape.com"

    def login(
        self,
        username: str = "testuser",
        password: str = "testpass",  # noqa: S107 - Test site accepts any password
    ) -> bool:
        """Log in to the site and cache the session.

        Args:
            username: Any username (site accepts anything).
            password: Any password (site accepts anything).

        Returns:
            True if login successful.
        """
        session = BrowserSession(backend="selenium", headless=False)
        session.driver.get(f"{self.base_url}/login")

        # Fill in login form
        session.driver.find_element("id", "username").send_keys(username)
        session.driver.find_element("id", "password").send_keys(password)
        session.driver.find_element("css selector", "input[type='submit']").click()

        # Verify login succeeded (page shows Logout link)
        session.driver.find_element("css selector", "a[href='/logout']")

        # Cache the authenticated session
        cache_session(session, self.session_name)
        session.quit()
        return True

    @command(help="List quotes from a page")
    def list(self, session, page: int = 1):
        """Fetch quotes from a page.

        Args:
            session: Authenticated requests session.
            page: Page number (default: 1).

        Returns:
            Dict with URL and status code.
        """
        response = session.get(f"{self.base_url}/page/{page}/")
        return {
            "url": response.url,
            "status": response.status_code,
            "page": page,
        }

    @command(help="Get a random quote")
    def random(self, session):
        """Fetch the random quote page.

        Args:
            session: Authenticated requests session.

        Returns:
            Dict with URL and status code.
        """
        response = session.get(f"{self.base_url}/random")
        return {
            "url": response.url,
            "status": response.status_code,
        }
