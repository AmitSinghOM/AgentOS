/** The one place the UI links out to the repository's docs. The UI ships inside the wheel and
 *  image without the docs tree, so a docs link has to point at the public repo, not a relative
 *  path. Pinned to `main`, which every release tag sits on. */
export const REPO = "https://github.com/AmitSinghOM/AgentOS";
export const TUTORIAL_URL = `${REPO}/blob/main/docs/tutorial.md`;
