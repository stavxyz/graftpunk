"""Quotes to Scrape plugin - declarative login example (Selenium).

Site: https://quotes.toscrape.com
Auth: Any username/password works (test site)

Usage:
    1. Symlink: ln -s $(pwd)/examples/plugins/quotes.py ~/.config/graftpunk/plugins/
    2. Login: gp quotes login
    3. Use: gp quotes list
"""

from graftpunk.plugins import CommandContext, LoginConfig, SitePlugin, command


class QuotesPlugin(SitePlugin):
    """Plugin for quotes.toscrape.com (test site, any credentials work)."""

    site_name = "quotes"
    session_name = "quotes"
    help_text = "Quotes to Scrape commands (test site)"
    base_url = "https://quotes.toscrape.com"
    backend = "selenium"
    api_version = 1

    login_config = LoginConfig(
        url="/login",
        fields={"username": "#username", "password": "#password"},
        submit="input[type='submit']",
        success="a[href='/logout']",
    )

    # token_config: No CSRF tokens needed for this test site.
    # For sites that require dynamic tokens, add:
    #
    #     from graftpunk.tokens import Token, TokenConfig
    #
    #     token_config = TokenConfig(tokens=(
    #         Token.from_meta_tag(name="csrf-token", header="X-CSRF-Token"),
    #     ))

    @command(help="List quotes from a page")
    def list(self, ctx: CommandContext, page: int = 1):
        """Fetch quotes from a page."""
        session = ctx.session
        response = session.get(f"{self.base_url}/page/{page}/")
        return {"url": response.url, "status": response.status_code, "page": page}

    @command(help="Get a random quote")
    def random(self, ctx: CommandContext):
        """Fetch the random quote page."""
        session = ctx.session
        response = session.get(f"{self.base_url}/random")
        return {"url": response.url, "status": response.status_code}
