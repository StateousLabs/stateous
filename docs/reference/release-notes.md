# Release notes

This page lists what each version of Stateous adds or changes, and what you do when you upgrade to it.

## 0.6.0

### New

- **Purge.** `purge` deletes a token that ended, and its child tokens, for good. It deletes the history, the tasks, the timers, the labels, the context and the memory. See [Purge](../concepts/pause-resume-cancel.md#purge).
  - Python: `ControlPlane.purge(token)` returns `Purged`. `Client.purge(token)` returns `{"token": ..., "purged": n}`.
  - HTTP: `POST /tokens/{id}/purge`.
  - Command: `stateous token purge ID`.
- **A new refusal code, `token_live`.** A purge of an active or a paused token gets it, with the status 409. In Python it is `TokenLive`.
- **A new authorizer action, `purge`.** The authorizer gets it with the token's labels.
- **A task id is never given again.** This is also true on SQLite now. A late report of a purged task cannot reach a new task.

### Upgrading to 0.6

The upgrade is one way. Take a backup of the database before you upgrade.

- 0.6.0 moves the schema of the database from `0001` to `0002`. `stateous serve` does it at its first start. For a `ControlPlane` in your own service, run `stateous migrate` in your deploy.
- On SQLite, the upgrade makes the table `tasks` again, with all its rows. It runs once, at the first start.
- On Postgres, the upgrade changes no table. It only records the version `0002`.
- After the upgrade, Stateous 0.5.x does not open the database, on SQLite and on Postgres. It refuses it, for example with `this database was made by another version of Stateous (schema 0002)`.
- So do not run 0.5.x and 0.6.0 on one database at the same time. Stop each 0.5.x control plane before a 0.6.0 one starts.
- To go back to 0.5.x, restore the backup that you took before the upgrade. Changes after the upgrade are then lost.
- Check your authorizer. An authorizer that allows each action that it does not name now also allows `purge`. Name `purge` in it.
- After you upgrade, write the skill again: `stateous install skill DIRECTORY --force`.

## See also

- [The commands](cli.md): `stateous migrate`.
- [Use Postgres](../how-to/use-postgres.md): what `serve` checks in the database at start.
- [Tenancy](../concepts/tenancy.md): the authorizer and its actions.
