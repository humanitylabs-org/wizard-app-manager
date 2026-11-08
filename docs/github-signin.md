# Sign in to GitHub from the Store

A friend invited to a private GitHub repository can sign in to GitHub from Apps → Sources, and the private beta apps they can see then install like any other app. The App Manager runs GitHub's [device flow](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps#device-flow) on the box. The browser never sees a token or the device code.

Code: `wam/github_signin.py`; routes in `StoreHandler` (`wam/cli.py`); tests: `tests/test_github_signin.py`.

## Setup: a GitHub OAuth App (one time, by the WizardOS org owner)

Nothing signs in until WizardOS has an OAuth App. Create it under `humanitylabs-org` (Settings → Developer settings → OAuth Apps), tick **Enable Device Flow**, and give its **client id** to the box. The client id is a public value; the device flow needs no client secret. Do not use another tool's client id (for example the `gh` CLI's).

The client id is read from, first found wins:

1. the environment: `WAM_GITHUB_CLIENT_ID`
2. `~/.wizard-os/app-manager.json`: `{"github_client_id": "Iv1...."}`

With neither, the state is `not_configured` and the Store shows that sign-in is not set up yet.

## Scope: what the sign-in can read

GitHub OAuth Apps have no read-only scope for private repositories. The flow therefore asks for **`repo`** (read the private app repositories the friend can see; the scope also allows writes) and **`read:packages`** (pull private images). The App Manager uses the token only on this box, for reads: fetching a repository at a commit, listing and reading `wizard.md`, pulling images, and asking `GET /user` for the username. It is never sent to a browser, an app container or an agent. Sign out deletes it, and the person can also revoke it at github.com/settings/applications.

## Routes

On the owner-gated store listener (`127.0.0.1:7791`, mounted at `/wizard-os-home/store-install/`). A POST passes the same gate as app removal: the owner's tailnet login, another tailnet device (not the box itself), the shell's own origin, JSON, `X-Wizard-Store: 1`. A GET needs the owner's login. The private-gateway listener (`:7792`) does not serve these paths.

| Request | Does |
|---|---|
| `GET _github` | `{state, login, source, user_code?, verification_uri?, expires_in?, interval?}` |
| `POST _github/start` `{}` | Begin sign-in; same shape, state `pending` with the code. Repeating it while pending returns the same code. `409` + `not_configured` when there is no client id. |
| `POST _github/signout` `{}` | Delete the saved token and username, end any sign-in in progress; returns the status. |

`state`: `signed_out`, `pending`, `signed_in`, `expired`, `denied`, `error`, `not_configured`. `login` is the GitHub username while `signed_in`, else `""`. `source` is `file` (saved by this sign-in, or a hand-placed `github-token`: Sign out removes it) or `env` (`GITHUB_TOKEN`, `GH_TOKEN` or the `gh` CLI: signed in, Sign out cannot remove it). `user_code`, `verification_uri`, `expires_in` and `interval` appear only while `pending`. A body other than `{}` is refused.

## How the flow runs

`start` asks GitHub for a device code and starts one background thread. The thread polls GitHub at its `interval`, adds its new interval or five seconds on `slow_down`, and stops on success, `expired_token` (or the code's own expiry), `access_denied`, any other GitHub error, or five failed polls in a row. The browser only polls `GET _github`. A sign-out while the thread is waiting stops it and nothing is saved.

On success the App Manager checks the token with `GET /user`, then writes the username and the token, each by an atomic 0600 write into a 0700 folder.

## Files (`~/.wizard-os/`)

| File | Holds | Notes |
|---|---|---|
| `github-token` | the access token | the file `wam/private.py` `token()` already reads; 0600, folder 0700 |
| `github-login` | the username only | 0600; never a token |

Both are per-machine: `wam/backup/box.py` `PER_MACHINE` lists them, so neither is captured in a backup or replaced by an import. `private.token()` still prefers `GITHUB_TOKEN`/`GH_TOKEN`, then this file, then `gh auth token`.

## Secrets handling

The token and device code appear in no response, log line, exception text or receipt, and not in git config. Errors reach the log as GitHub's short error code or an exception type name only. GitHub redirects are not followed and answers are capped at 64 KiB. Tests use fake tokens against a local mock GitHub.
