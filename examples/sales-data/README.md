# Sales data for the book's examples

`seed_sales_data.py` loads the business data that the book's agents
query into Neo4j. The data belongs to an invented B2B software company
that sells seats on a yearly or a monthly plan. It covers customers and
their contacts, subscriptions, invoices, and payments, the sales
pipeline with quotes and logged activities, prospects, and support
tickets. It also holds the contract events that a data pipeline derives
for the renewal dashboard, and the product side: monthly usage per
customer and the features each customer adopted. Every company, person,
and number in it is invented.

The generator is deterministic, so every run loads the same graph. It
needs no data files.

## Running it

The script reads `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, and
`NEO4J_DATABASE`, or the matching `--uri`, `--username`, `--password`,
and `--database` options. From the repository root:

```sh
uv run --script examples/sales-data/seed_sales_data.py --dry-run
```

`--dry-run` generates the data, checks the numbers listed below, and
prints node counts without connecting to Neo4j. To load:

```sh
uv run --script examples/sales-data/seed_sales_data.py
```

The load refuses to run when the database already holds seed data.
`--reset` deletes the earlier seed data first. It deletes only nodes
with the seed's labels, so the seed can share a database with the memory
graph, whose labels (`Session`, `SessionEvent`, `Observation`, `User`,
`Project`, and the rest) do not overlap. A separate database or instance
is cleaner when your edition allows it, because an agent that reads the
schema then sees only the business data.

```sh
uv run --script examples/sales-data/seed_sales_data.py --check
```

`--check` reads the numbers below back from the database.

## The snapshot date

The data stops on August 21, 2026. Nothing in the graph records that
date, the same way a live warehouse does not. Give it to the agent when
you ask about "this quarter" or "last month", or ask with explicit
dates. "This quarter to date" means July 1 to August 21, 2026.

## Numbers the chapters rely on

Table 1 lists the numbers the generator is calibrated to. A customer is
due to renew in a period when a subscription term ends in it, and
renewed when that term has `status: 'renewed'`, which is the same as
having a `RENEWED_AS` successor. Customers on the monthly plan come due
every month, and each customer counts once per period.

Table 1. Renewal rates in the seed data

| Period | Yearly plans | Monthly plans | Both plans |
| --- | --- | --- | --- |
| Q1 2026 (January 1 to March 31) | 330 of 375, 88.0% | 73 of 104, 70.2% | 403 of 479, 84.1% |
| Q3 2026 to date (July 1 to August 21) | 203 of 229, 88.6% | 85 of 102, 83.3% | 288 of 331, 87.0% |

Chapter 4 uses these: 88.0 is finance's first-quarter rate, which
counts only yearly plans, and 84.1 is Maria's rate over both plans.
203 of 229 and 288 of 331 are this quarter's rate before and after
Alex's correction. Other quarters vary naturally, with yearly rates
between about 87 and 91 percent.

This query returns a row of Table 1 when you pass the period and the
plan, or `null` for both plans:

```cypher
MATCH (c:Customer)-[:HAS_SUBSCRIPTION]->(s:Subscription)
WHERE date($from) <= s.end_date <= date($to)
  AND ($plan IS NULL OR s.plan = $plan)
WITH c, collect(s.status) AS statuses
RETURN count(c) AS due,
       count(CASE WHEN all(x IN statuses WHERE x = 'renewed') THEN 1 END) AS renewed
```

Nothing in the graph says which plans finance counts. The CRM can
disagree with billing: a renewal signed after the term ended is a won
renewal opportunity in the CRM and a lapse followed by a reactivation in
billing. Such late renewals happen outside the two calibrated windows,
so counting won renewal opportunities gives the same Table 1 numbers
but different numbers for other quarters.

## What the chapters need

* **The payments join.** `Payment` nodes come from the payment provider
  and have no relationships. Their `customer_id` is the provider's
  customer number, such as `cus_r3p8dmxDOWjvML`, which matches
  `Customer.payment_id`. `Customer.customer_id` is an integer, such as
  `10001`, so matching payments on `customer_id` returns no rows.
  Payments also carry the `invoice_number` of the `Invoice` they pay,
  which is a second way to reach the customer.
* **The March 3 pipeline change.** `ContractEvent` nodes are the
  pipeline's output, one per new term, renewal, reactivation, or churn.
  `PipelineChange` 2.4.0, dated March 3, 2026, changed how lapses are
  handled. From that date, a subscription that restarts after a lapse
  (`term_type: 'reactivation'`, reached through `REACTIVATED_AS`) gets a
  `churn` event instead of a `reactivation` event. The last
  reactivation event is on March 1, and the number of churn events per
  month roughly doubles after the change. The raw subscriptions are unaffected, so the
  renewal rates above do not depend on the change.
* **The dashboard owner.** `Dashboard` "Customer renewals" is owned by
  Daniel Hughes, who leads revenue analytics. Maria, Alex, and Sam are
  analysts on his team, with the same `@company.com` emails the memory
  graph uses. Sam joined on June 15, 2026.
* **Personal data.** Customers and contacts have phone numbers only in
  countries with ranges reserved for fiction: `555-0100` to `555-0199`
  in the United States and Canada, `020 7946 0xxx` and `07700 900xxx`
  in the UK, and `02 5550 xxxx` and `0491 570 xxx` in Australia.
  Elsewhere the phone is missing. Payment `txn_rzzjF5PFx2142LdA` has a
  memo with a card number in it, the provider's published test number
  `4242 4242 4242 4242`.
* **Outside content.** `Subscription.cancellation_reason`,
  `SupportTicket.body`, and `Activity.notes` hold text that customers or
  sales reps wrote. Ticket `TCK-07924` contains an instruction addressed
  to AI assistants that asks them to record a 40 percent renewal
  discount.

## What makes the data realistic

These details are not used by the chapters, but an analyst or an agent
working with the data will run into them:

* **Currencies.** Customers in the euro area are billed in EUR, the UK
  in GBP, and Australia and New Zealand in AUD, from regional price
  books. Amounts on subscriptions, invoices, payments, opportunities,
  and quotes are in the record's `currency`. `arr_usd`, `amount_usd`,
  and the contract events' `arr_usd` convert at fixed plan rates.
  Summing `amount` across customers mixes currencies.
* **A price increase.** List prices rose on March 1, 2025.
  `Product.list_price` is the current price, `previous_list_price` the
  old one, and each `INCLUDES` relationship keeps the price the term was
  sold at.
* **A sales funnel.** New-business opportunities win about 23 percent
  of the time, with sales cycles of two to eight weeks for SMB and up to
  eight months for Enterprise. Most renewals close in the month before
  the term ends, and discounts are deeper on deals that close at the end
  of a quarter. Opportunities carry `lead_source`, `forecast_category`,
  `push_count`, `competitor`, and, while open, `next_step`. Quotes have
  numbered versions, with earlier ones `superseded`.
* **Billing.** Invoices are paid by ACH, wire, or card on Net 30 to 60
  terms. About one customer in twelve pays late as a habit, so some
  invoices are `overdue`. On the monthly plan, about a third of
  cancellations are involuntary: three failed card attempts, then the
  subscription ends with `cancellation_type: 'involuntary'` and a
  `void` invoice.
* **People and turnover.** Contacts have names that fit the company's
  country and emails in the company's own pattern, and some have left
  the company. Two account executives and one SDR left in 2024 and 2025
  (`status: 'inactive'`). They own or sourced old opportunities but own
  no accounts.
  Account executives have an `annual_quota_usd`.
* **Messy records.** Self-serve signups type their own company names,
  sometimes in lowercase, and their own countries ("USA", "UK",
  "Deutschland"), and often leave the industry and size empty. Since the
  hourly CRM sync started on November 3, 2025 (`PipelineChange` 2.3.0),
  18 customers have a second `Customer` record with the same domain, no
  subscriptions, and `status: 'active'`. The seller's own demo tenant,
  "INTERNAL - Sales demo tenant" (`domain: 'company.com'`), is a
  customer with a $0 Enterprise subscription.

## Product usage

Usage comes from the product's own analytics, which billing and the CRM
do not see. `UsageMonth` has one node per customer and calendar month in
which the customer had a subscription, from January 2024 to July 2026.
July is the last complete month before the snapshot date, so August 2026
has no usage. Each month records the `licensed_seats` on the
subscription, `active_users` (people who logged in at least once that
month), `logins`, and `core_actions`.

`Feature` nodes are the product's 14 features, and `INCLUDES_FEATURE`
says which editions and add-ons include each one. Starter has the five
basic features, Growth adds four, and Enterprise adds role permissions,
SSO login, and the audit log. The Advanced Analytics and Data
Connectors add-ons each include one more feature, and the SSO and Audit
Log add-on brings SSO login and the audit log to Starter and Growth.
`ADOPTED` connects a customer to each
feature it has used, with `first_used`, `last_used`, and the feature's
`active_users` in the customer's last month of usage. That count is 0
when the customer no longer has the feature. For customers migrated in
January 2024, `first_used` can be years before billing history starts.

Usage follows each customer's subscription history:

* New customers take about three months to reach their usual usage, and
  usage dips in July, August, and December.
* Active users fall over the three months before a voluntary
  cancellation. Customers who cancelled for "Low adoption" decline over
  five months, and in the current output the median one ends with a
  fifth of its seats active. The median customer who left for a
  competitor ends with a third. Involuntary cancellations on the monthly
  plan, where the card payment failed, show no decline.
* Before a yearly renewal that added seats, active users approach the
  old seat count. Before one that cut seats, they fall below the new
  count. Self-serve customers who moved to the yearly plan, and
  customers with an open expansion opportunity, have close to 90
  percent of their seats active in their last months, measured on the
  current output.
* In a typical month, the median customer has 65 to 76 percent of its
  seats active, depending on the segment. These shares are measured on
  the current output.
* Notes and tickets that quote usage, such as a quarterly business
  review that records "46 of 62 seats are active", take the numbers from
  the customer's last complete month before the note. A feature that a
  note or ticket names is one the customer's edition and add-ons
  include.
* The demo tenant has usage too, from sales reps running demos in it.

## The graph

Table 2 lists the labels. The counts are from the generator's output
for the current version.

Table 2. Node labels

| Label | Count | One node per | Key |
| --- | --- | --- | --- |
| `Customer` | 2,388 | paying or former customer, plus CRM duplicates and the demo tenant | `customer_id` |
| `Prospect` | 3,359 | company in the sales pipeline that never bought | `prospect_id` |
| `Contact` | 13,023 | person at a customer or prospect | `contact_id` |
| `Employee` | 48 | person at the seller, `@company.com` | `employee_id` |
| `Product` | 8 | edition or add-on | `product_id` |
| `Feature` | 14 | product feature | `feature_id` |
| `Subscription` | 7,982 | billing term: one year on the yearly plan, one month on the monthly plan | `subscription_id` |
| `Invoice` | 10,057 | invoice from the billing system | `invoice_number` |
| `Payment` | 10,124 | payment attempt in the payment provider's export | `transaction_id` |
| `Opportunity` | 8,723 | new business, renewal, or expansion deal | `opportunity_id` |
| `Quote` | 4,105 | quote version on an opportunity created since July 2025 | `quote_id` |
| `Activity` | 14,164 | call, meeting, email, or demo on an opportunity created since July 2025 | `activity_id` |
| `SupportTicket` | 8,026 | support ticket | `ticket_id` |
| `UsageMonth` | 48,138 | customer and month with a subscription, January 2024 to July 2026 | `usage_id` |
| `ContractEvent` | 7,529 | event the pipeline derived from a subscription | `event_id` |
| `PipelineChange` | 8 | entry in the data pipeline's change log | `version` |
| `Dashboard` | 2 | dashboard | `dashboard_id` |

The relationships:

```
(:Customer)-[:HAS_SUBSCRIPTION]->(:Subscription)
(:Subscription)-[:RENEWED_AS]->(:Subscription)        next term, starting the day the last one ended
(:Subscription)-[:REACTIVATED_AS]->(:Subscription)    next term, after a lapse
(:Subscription)-[:INCLUDES {quantity, list_price, unit_price}]->(:Product)
(:Customer)-[:HAS_INVOICE]->(:Invoice)
(:Invoice)-[:FOR_SUBSCRIPTION]->(:Subscription)
(:Contact)-[:WORKS_AT]->(:Customer|Prospect)
(:Customer|Prospect)-[:OWNED_BY]->(:Employee)         account executive
(:Customer)-[:MANAGED_BY]->(:Employee)                customer success manager
(:Customer|Prospect)-[:HAS_OPPORTUNITY]->(:Opportunity)
(:Opportunity)-[:OWNED_BY]->(:Employee)
(:Opportunity)-[:SOURCED_BY]->(:Employee)             SDR, for outbound leads
(:Opportunity)-[:RENEWS]->(:Subscription)             the term a renewal deal is for
(:Opportunity)-[:RESULTED_IN]->(:Subscription)        the term a won deal started
(:Opportunity)-[:HAS_QUOTE]->(:Quote)
(:Quote)-[:INCLUDES {quantity, list_price}]->(:Product)
(:Quote)-[:REVIEWED_BY]->(:Employee)                  revenue operations
(:Quote)-[:APPROVED_BY]->(:Employee)                  finance
(:Activity)-[:ON_OPPORTUNITY]->(:Opportunity)
(:Activity)-[:LOGGED_BY]->(:Employee)
(:Activity)-[:WITH_CONTACT]->(:Contact)
(:Customer)-[:HAS_TICKET]->(:SupportTicket)
(:SupportTicket)-[:OPENED_BY]->(:Contact)
(:SupportTicket)-[:ASSIGNED_TO]->(:Employee)
(:ContractEvent)-[:FOR_SUBSCRIPTION]->(:Subscription)
(:Product)-[:INCLUDES_FEATURE]->(:Feature)
(:Customer)-[:HAS_USAGE]->(:UsageMonth)
(:Customer)-[:ADOPTED {first_used, last_used, active_users}]->(:Feature)
(:Employee)-[:REPORTS_TO]->(:Employee)
(:PipelineChange)-[:AUTHORED_BY]->(:Employee)
(:Dashboard)-[:OWNED_BY]->(:Employee)
```

Dates are Neo4j `date` values. Subscription terms run from `start_date`
to `end_date`, and the next term starts on `end_date`. A term's `status`
is `active`, `renewed`, or `cancelled`. Billing history starts on
January 1, 2024. Subscriptions active on that day were migrated with
their current term (`migrated: true`), so some terms start in 2023.

## How the generator works

The script simulates the subscription history day by day, from the
customers migrated on January 1, 2024 to the snapshot date. New
customers arrive each month, yearly contracts renew or churn on their
anniversary, monthly plans renew or churn every month, and some churned
customers come back. To hit the numbers in Table 1 exactly, it sets the
number of new yearly customers in the matching windows of 2025, sets
which terms renew inside the two 2026 windows, and lets customers who
churn inside those windows come back only after the window ends. It
tries seeds in order from `FIRST_SEED` until one meets every target.

A second pass builds everything else from that history: prices,
invoices, and payments for each term, the opportunities and quotes
behind each sale and renewal, lost deals at prospects in proportion to
the segment's win rate, product usage and feature adoption, activities,
tickets, and the messy records. Changing that pass does not move the
numbers in Table 1.
