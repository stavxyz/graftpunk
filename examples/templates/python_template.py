"""Python Plugin Template.

Copy this file and customize for your site.
Save or symlink to: ~/.config/graftpunk/plugins/<name>.py

Documentation: https://github.com/stavxyz/graftpunk
"""

from graftpunk import BrowserSession, cache_session
from graftpunk.plugins import SitePlugin, command


class MySitePlugin(SitePlugin):
    """Plugin for MySite.

    Replace this with a description of what your plugin does.
    """

    # CLI command group: gp mysite <cmd>
    site_name = "mysite"

    # Cached session name (must match what you pass to cache_session)
    session_name = "mysite"

    # Help text shown in CLI
    help_text = "Description of your plugin"

    # Base URL for the site
    base_url = "https://example.com"

    def login(self, username: str, password: str) -> bool:
        """Log in to the site and cache the session.

        Customize this method for your site's login flow.

        Args:
            username: Your username.
            password: Your password.

        Returns:
            True if login successful.
        """
        # Choose backend: "selenium" or "nodriver"
        session = BrowserSession(backend="selenium", headless=False)
        session.driver.get(f"{self.base_url}/login")

        # Fill login form - customize selectors for your site
        # session.driver.find_element("id", "username").send_keys(username)
        # session.driver.find_element("id", "password").send_keys(password)
        # session.driver.find_element("id", "submit").click()

        # Wait for login to complete - customize selector
        # session.driver.find_element("css selector", "a[href='/logout']")

        # Cache the authenticated session
        cache_session(session, self.session_name)
        session.quit()
        return True

    @command(help="Example command")
    def example(self, session, param: str = "default"):
        """Example command that makes an API request.

        Args:
            session: Authenticated requests session (injected automatically).
            param: Example parameter with default value.

        Returns:
            Response data (will be formatted as JSON by default).
        """
        response = session.get(f"{self.base_url}/api/endpoint")
        return response.json()

    # Add more commands as needed:
    #
    # @command(help="List items")
    # def list(self, session, page: int = 1):
    #     response = session.get(f"{self.base_url}/api/items?page={page}")
    #     return response.json()
    #
    # @command(help="Get item by ID")
    # def get(self, session, item_id: int):
    #     response = session.get(f"{self.base_url}/api/items/{item_id}")
    #     return response.json()
