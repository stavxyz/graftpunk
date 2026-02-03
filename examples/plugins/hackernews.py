"""Hacker News plugin - declarative login example (NoDriver).

Site: https://news.ycombinator.com
Auth: Requires real Hacker News account

Usage:
    1. Symlink: ln -s $(pwd)/examples/plugins/hackernews.py ~/.config/graftpunk/plugins/
    2. Login: gp hn login
    3. Use: gp hn front
"""

from graftpunk.plugins import CommandContext, LoginConfig, SitePlugin, command


class HackerNewsPlugin(SitePlugin):
    """Plugin for Hacker News (news.ycombinator.com)."""

    site_name = "hn"
    session_name = "hackernews"
    help_text = "Hacker News commands"
    base_url = "https://news.ycombinator.com"
    backend = "nodriver"
    api_version = 1

    login_config = LoginConfig(
        url="/login",
        fields={"username": "input[name='acct']", "password": "input[name='pw']"},
        submit="input[value='login']",
        failure="Bad login.",
    )

    # token_config: No CSRF tokens needed for Hacker News.
    # For sites that require dynamic tokens, add:
    #
    #     from graftpunk.tokens import Token, TokenConfig
    #
    #     token_config = TokenConfig(tokens=(
    #         Token.from_meta_tag(name="csrf-token", header="X-CSRF-Token"),
    #     ))

    @command(help="Get front page stories")
    def front(self, ctx: CommandContext, page: int = 1):
        """Fetch front page stories."""
        session = ctx.session
        response = session.get(f"{self.base_url}/news?p={page}")
        return {"url": response.url, "status": response.status_code, "page": page}

    @command(help="Get newest stories")
    def newest(self, ctx: CommandContext, page: int = 1):
        """Fetch newest stories."""
        session = ctx.session
        response = session.get(f"{self.base_url}/newest?p={page}")
        return {"url": response.url, "status": response.status_code, "page": page}

    @command(help="Get saved stories (requires login)")
    def saved(self, ctx: CommandContext):
        """Fetch your saved stories."""
        session = ctx.session
        user_cookie = session.cookies.get("user", "")
        username = user_cookie.split("&")[0] if user_cookie else ""
        if not username:
            return {"error": "Not logged in. Run: gp hn login"}
        response = session.get(f"{self.base_url}/favorites?id={username}")
        return {"url": response.url, "status": response.status_code}
