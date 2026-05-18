import { randomUUID } from "crypto";
import GitHubProvider from "next-auth/providers/github";
import CredentialsProvider from "next-auth/providers/credentials";
import NextAuth, { type NextAuthOptions } from "next-auth";
import {
  User as NextAuthUser,
  Account as NextAuthAccount,
  Profile as NextAuthProfile,
} from "next-auth";
import config from "../../../../env.config";

interface signInArgs {
  user: NextAuthUser;
  account: NextAuthAccount;
  profile: NextAuthProfile;
}

const authOptions: NextAuthOptions = {
  // Fallback secret lets CredentialsProvider work in the sandbox without a
  // NEXTAUTH_SECRET env var.  Replace with a real secret in production.
  secret: process.env.NEXTAUTH_SECRET ?? "dorian-sandbox-secret-not-for-production",
  providers: [
    // GitHub OAuth — only register when both credentials are
    // non-empty. Registering the provider with an empty
    // ``clientId`` makes NextAuth crash the sign-in route with
    // ``OAuthSignin: client_id is required`` (the provider's
    // GitHub config is fetched at sign-in time, not at register
    // time, so the empty-string credential propagates all the way
    // to GitHub's authorization endpoint). Skip registration
    // instead — the sign-in UI hides the GitHub button when the
    // provider isn't available.
    ...(config.GITHUB_ID && config.GITHUB_SECRET
      ? [
          GitHubProvider({
            clientId: config.GITHUB_ID,
            clientSecret: config.GITHUB_SECRET,
          }),
        ]
      : []),
    // ---------------------------------------------------------------------------
    // Sandbox / preview mock provider — bypasses GitHub OAuth callback.
    // Remove or gate behind an env flag before production deployment.
    // ---------------------------------------------------------------------------
    CredentialsProvider({
      id: "demo",
      name: "Demo (sandbox)",
      credentials: {
        username: { label: "Username", type: "text", placeholder: "demo" },
      },
      async authorize(credentials) {
        // Each demo login gets a unique ID so users don't share
        // sessions, datasets, pipelines, or vault secrets.
        const demoId = `demo-${randomUUID()}`;
        const displayName = credentials?.username?.trim() || "Demo User";
        return {
          id: demoId,
          name: displayName,
          email: `${demoId}@dorian.local`,
          image: null,
        };
      },
    }),
  ],
  callbacks: {
    async jwt({ token, profile, account, user }) {
      // Persist the auth provider on the token so the session callback can
      // tell a real GitHub user from a sandbox demo user.  `account` is only
      // present on initial sign-in — subsequent reads carry the token from
      // the previous jwt() invocation.
      if (account?.provider) {
        token.provider = account.provider;
      }
      if (account?.provider === "github" && profile) {
        // GitHub profile has `login` (the @username) on the raw profile.
        // Only GitHub-authenticated users can ever be admins — the demo
        // provider must NEVER be able to populate this field (otherwise a
        // sandbox user could type an admin username into the credentials
        // form and impersonate them).
        token.login = (profile as Record<string, unknown>).login as string;
      }
      // Demo users: bind login to the server-generated demo ID so it's
      // impossible to collide with any GitHub login listed in
      // config.admin.usernames.
      if (account?.provider === "demo" && user) {
        token.login = user.id; // `demo-<uuid>` — not a valid admin username
      }
      return token;
    },
    async session({ session, token }) {
      if (session.user) {
        session.user.id = token.sub;
        // Expose the auth-time login as `session.user.login`.  For GitHub
        // users this is their @username; for demo users this is the
        // server-generated `demo-<uuid>`.  We NEVER fall back to
        // `session.user.name` because that field is controlled by the
        // credentials form and could be forged into an admin username.
        (session.user as Record<string, unknown>).login = token.login ?? null;
        (session.user as Record<string, unknown>).provider = token.provider ?? null;
      }
      return session;
    },
  },
};

const handler = NextAuth(authOptions);

export { handler as GET, handler as POST };
