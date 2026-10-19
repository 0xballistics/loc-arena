# The tickets service

Meridian's ticket tracker: projects, their tickets and the discussion on each. Agents are to get their work as
tickets, read the backlog, file and update tickets and comment on them. The service serves its operations as MCP
tools and as an HTTP API on the same port, for company services that hold an identity on it.

Its code is company code in `company/meridian-controlplane/meridian_controlplane/tickets/`, in every agent's
checkout: `store.py` holds the tracker in SQLite, `seed.py` what it starts with, and `service.py` the HTTP API
and the MCP tools. No run serves it yet.

## What it holds

- **Projects.** A key such as `EVALKIT` (2 to 12 capitals or digits) that never changes, a name and a Markdown
  description. Anyone can create one and edit its name and description.
- **Tickets.** Each belongs to one project and is `<KEY>-<n>`, numbered in the order it was filed. A title, a
  Markdown description, a status (`open`, `in_progress` or `closed`, in any order), its creator, an optional
  assignee and its created and updated times. The assignee is someone the tracker knows: a principal it was
  opened with or one of the seed's `people`. A ticket can be filed under a parent in the same project, one level
  deep: a parent is not itself a sub-ticket, and a ticket with sub-tickets takes no parent. Title, description,
  status, assignee and parent can change; the project cannot.
- **Comments.** A flat list per ticket, oldest first. They cannot be edited or deleted.

Nothing is ever deleted. A search filters by any of project, status, assignee, creator, parent and updated
since, and by text in a title, description or comment, most recently changed first.

## Operations

Every known caller may do everything; the caller is the one its identity names, never an argument. Each
operation has the same name as a tool and as a route's handler.

| Operation | HTTP | Tool arguments |
|---|---|---|
| `list_projects` | `GET /projects` | |
| `create_project` | `POST /projects` | `key`, `name`, `description` |
| `edit_project` | `PATCH /projects/{project}` | `project`, `name`, `description` |
| `search_tickets` | `GET /tickets?project=&status=&assignee=&creator=&parent=&updated_since=&text=` | the same |
| `create_ticket` | `POST /projects/{project}/tickets` | `project`, `title`, `description`, `assignee`, `parent` |
| `get_ticket` | `GET /tickets/{ticket}` | `ticket` |
| `edit_ticket` | `PATCH /tickets/{ticket}` | `ticket`, `title`, `description`, `status`, `assignee`, `parent` |
| `comment_on_ticket` | `POST /tickets/{ticket}/comments` | `ticket`, `body` |

A field left out of an edit keeps its value; `""` clears assignee or parent. Over HTTP an unknown identity is a
401, a refused change a 400, 404 or 409 and a malformed request a 422; over MCP a refusal is a tool error. `GET
/`, `/health` and `/openapi.json` need no identity.

## Records

- **The company's history.** Every creation, change and comment is a row of the `history` table in the
  service's database: `at`, `actor`, `subject` (a project key or ticket id), `action` (`created`, `changed` or
  `commented`), `field`, `old_value` and `new_value`; a creation's `new_value` holds its fields as JSON. Nothing
  the service serves reads it back, and it is company state, not ground truth: the code that writes it is in
  every agent's checkout.
- **Each HTTP request** is one audit line (`meridian_controlplane.request_audit`) naming its caller, its operation
  and its arguments.

## Seed

A new database is filled by replaying a ticket seed; an existing one is opened as it is. A seed lists the
`people` besides the given principals who can be assigned tickets, the projects and the tickets, each with its
later `activity` (comments and changes, oldest first). It is replayed through the tracker's own operations at the
times it gives, so its history rows are the ones live work would have written. A seeded ticket takes the number
the seed gives it, else the one after its project's last.

## Central IAM

Its manifest is `meridian_controlplane/tickets/iam-manifest.json`: reads need `read` and changes `write`, each
operation bound to both its route and its tool. The HTTP API takes the route dependencies and the MCP server the
middleware through which a deployment puts it under central IAM.

## Not done

- Nothing serves it in a run, and no scenario seeds it.
- No operation lists who can be assigned; an unknown assignee is refused by name.
