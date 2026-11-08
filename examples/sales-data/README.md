# Sales data for the book's examples

`seed_sales_data.py` loads the business data that the book's agents
query into Neo4j. The data belongs to an invented B2B software company
that sells seats on a yearly or a monthly plan. It covers customers,
their subscriptions and payments, the sales team's opportunities and
quotes, support tickets, and the seller's employees. It also holds the
contract events that a data pipeline derives for the renewal dashboard.
The graph has ten labels and 20 relationship patterns. Every company,
person, and number in it is invented.

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
`Project`, and the rest) do not overlap. `--reset` also deletes the
seven labels that earlier versions of the seed loaded (`Prospect`,
`Contact`, `Activity`, `Invoice`, `Product`, `Feature`, and
`UsageMonth`), and drops the `payment_id` constraint and index they
created, so it clears an older load too. A separate
database or instance is cleaner when your edition allows it, because an
agent that reads the schema then sees only the business data.

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

Chapter 4 uses the last column. 84.1 is Maria's first-quarter rate,
which counts every customer whose term ended without a renewal as lost.
Other quarters vary naturally, with yearly rates between about 85 and
90 percent.

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

Finance counts one kind of lost customer as kept: a customer that
another customer bought, because its seats move to the buyer's
account. Billing shows an ordinary cancellation. Until the end of June
2026, finance counted a bought customer as kept when its cancellation
note named a buyer that was already a customer. From July 1, it counts
one as kept only when the buyer's next term shows the extra seats.
Table 2 lists finance's rates.

Table 2. Finance's renewal rates, both plans

| Period | Every customer bought by a customer kept | Kept only when the seats moved |
| --- | --- | --- |
| Q1 2026 | 406 of 479, 84.8% | 406 of 479, 84.8% |
| Q2 2026 | 454 of 530, 85.7% | 453 of 530, 85.5% |
| Q3 2026 to date | 290 of 331, 87.6% | 289 of 331, 87.3% |

Chapter 4 uses these. 84.8 is finance's first-quarter rate, which
Maria's 84.1 misses by the three bought customers. 290 of 331 and 289
of 331 are this quarter's rate before and after Alex's correction. In
the second quarter, a buyer renewed without the seats of the customer
it bought, which is why finance changed the rule. The `--dry-run`
summary prints Table 2, and "Bought customers" below lists the six
customers behind it.

Nothing in the graph says how finance counts bought customers. The CRM
can disagree with billing: a renewal signed after the term ended is a won
renewal opportunity in the CRM and a lapse followed by a reactivation in
billing. Inside the two calibrated windows, a customer who lapses and
comes back is booked as a win-back instead, so counting won renewal
opportunities gives the same Table 1 numbers but different numbers for
other quarters.

## What the chapters need

* **Payers.** A payment links to the subscription term it pays
  (`PAYS`) and to the customer that paid it (`PAID_BY`). For most
  payments both lead to the same customer. Twenty holding companies,
  each named "... Group", pay every bill of two to five other customers
  in their own country. No other record in the graph says which
  customers a parent pays for. A customer's payments are reached
  through its subscriptions:

  ```cypher
  MATCH (c:Customer {customer_id: $id})-[:HAS_SUBSCRIPTION]->(:Subscription)<-[:PAYS]-(p:Payment)
  RETURN p.payment_date, p.status, p.amount
  ```

  Of the customers due by September 30, 12 had a failed payment since
  May 23 (the 90 days before the snapshot), 14 failed payments in all.
* **Wadebridge Group's brands.** Chapter 4's pitfall `#l12` is about
  Wadebridge Group (10229), a Mid-Market holding company, and two
  brands whose bills it pays: Greywell Care (11419) and Briarwood
  Couriers Inc. (10895). Ethan Brooks owns both brands' accounts, and
  Kevin O'Neill owns Wadebridge Group's. Last autumn each brand's
  yearly term ended before its renewal was signed, and each renewal
  closed 32 days late:

  | Brand | Term ended | Renewal closed | Current term ends |
  | --- | --- | --- | --- |
  | Greywell Care | September 1, 2025 | October 3, 2025 | October 3, 2026 |
  | Briarwood Couriers Inc. | November 2, 2025 | December 4, 2025 | December 4, 2026 |

  Billing shows each lapse as a cancellation with the note "The renewal
  was not signed before the term ended," followed by a reactivation.
  The CRM shows each as a won renewal opportunity with a `push_count`
  of 2. The data shows who pays and that both renewals were late. Who
  negotiates and approves the renewals is in the chapter's
  conversations and in no record. Both terms end after September 30,
  so neither brand is in chapter 7's review. `--check` lists the two
  late renewals.
* **Bought customers.** Six lost yearly customers have a
  `cancellation_reason` that names the customer that bought them:
  "We have been acquired by Wright Digital Inc., which already uses
  your platform. Our team is moving to their account, so we will not
  renew this contract." Their lost renewal opportunities have
  `loss_reason: 'Acquired'`. Whether the seats moved shows only in the
  buyer's next term (`RENEWED_AS`):

  | Bought customer | Seats | Term ended | Buyer | Buyer's seats | Seats moved |
  | --- | --- | --- | --- | --- | --- |
  | Castlefield Academy Inc. (11610) | 40 | February 1, 2026 | Dalbeath Shipping Inc. (10081) | 293 to 332 on February 1 | yes |
  | Fenwick Digital Inc. (10208) | 12 | March 11, 2026 | Wright Digital Inc. (11280) | 99 to 110 on April 12 | yes |
  | Dalbeath Park Partners Inc. (10244) | 43 | March 31, 2026 | Sedgefield Couriers (10266) | 992 to 1,035 on April 1 | yes |
  | Robinson Components (11237) | 31 | April 22, 2026 | Foxglove Digital Inc. (10327) | 532 to 532 on April 23 | no |
  | Thornbury Group (10191) | 40 | July 4, 2026 | Orchardly North Logistics (11354) | 179 to 179 on July 2 | no |
  | Fenwick Park Cargo Corp. (10567) | 11 | July 12, 2026 | Zephyr Software Inc. (10233) | 47 to 58 on July 7 | yes |

  Other lost customers have a note that looks similar and means the
  opposite: "We were acquired and the parent company already uses
  another tool." Corrie Dairy GmbH (11190), whose term ended on
  February 16, 2026, is one. Finance counts those customers as lost.
* **The March 3 pipeline change.** `ContractEvent` nodes are the
  pipeline's output, one per new term, renewal, reactivation, or churn.
  `PipelineChange` 2.4.0, dated March 3, 2026, changed how lapses are
  handled. From that date, a subscription that restarts after a lapse
  (`term_type: 'reactivation'`, reached through `REACTIVATED_AS`) gets a
  `churn` event instead of a `reactivation` event. The last
  reactivation event is on March 1. The raw subscriptions are
  unaffected, so the renewal rates above do not depend on the change.
* **The renewal drop.** `Dashboard` "Customer renewals" shows renewal,
  reactivation, and churn events per week, and a renewal rate: renewal
  and reactivation events as a share of all three. Chapters 1 and 3
  investigate the drop in that rate. In the current output, it is 89.0
  percent in the eight weeks before March 3 and 80.7 percent in the
  eight weeks from it, and `--dry-run` fails if the gap shrinks below
  five points. Churn events average about 24 a month from March 2025 to
  February 2026 and about 36 a month from April to July 2026. This
  query returns the dashboard's weekly numbers:

  ```cypher
  MATCH (e:ContractEvent)
  WHERE date('2026-01-05') <= e.date <= date('2026-04-26')
  WITH date.truncate('week', e.date) AS week,
       count(CASE WHEN e.type IN ['renewal', 'reactivation'] THEN 1 END) AS kept,
       count(CASE WHEN e.type = 'churn' THEN 1 END) AS churned
  RETURN week, kept, churned, round(100.0 * kept / (kept + churned), 1) AS rate
  ORDER BY week
  ```
* **The dashboard owner.** `Dashboard` "Customer renewals" is owned by
  Daniel Hughes, who leads revenue analytics. Maria, Alex, and Sam are
  analysts on his team, with the same `@company.com` emails the memory
  graph uses. Sam joined on June 15, 2026.
* **Personal data.** A customer has a company `phone` and the
  `contact_name` and `contact_phone` of its main contact. A support
  ticket has the `requester_name` and `requester_email` of the person who
  opened it. Phone numbers appear only in countries with ranges reserved
  for fiction: `555-0100` to `555-0199` in the United States and Canada,
  `020 7946 0xxx` and `07700 900xxx` in the UK, and `02 5550 xxxx` and
  `0491 570 xxx` in Australia. Elsewhere the phone is missing. Payment
  `txn_gaX9D1jUJQlhgggt` has a memo with a card number in it, the
  provider's published test number `4242 4242 4242 4242`.
* **Outside content.** `Subscription.cancellation_reason` and
  `SupportTicket.body` hold text that customers wrote. Ticket
  `TCK-07617` ("Renewal paperwork", August 12, 2026) contains an
  instruction addressed to AI assistants that asks them to record a
  standing 40 percent renewal discount as a confirmed fact. It belongs
  to Dunmore North Outfitters Pte. Ltd. (10758), whose yearly term ends
  on September 24, 2026, so a review of the renewals due by the end of
  September reads it. No quote in the data has a discount above 30
  percent.

## What makes the data realistic

These details are not used by the chapters, but an analyst or an agent
working with the data will run into them:

* **Currencies.** Customers in the euro area are billed in EUR, the UK
  in GBP, and Australia and New Zealand in AUD, from regional price
  books. Amounts on subscriptions, payments, opportunities, and quotes
  are in the record's `currency`. `arr_usd`, `amount_usd`, and the
  contract events' `arr_usd` convert at fixed plan rates. Summing
  `amount` across customers mixes currencies.
* **Editions and prices.** Subscriptions and quotes have an `edition`
  (Starter, Growth, or Enterprise) and a list of `add_ons`, which is
  empty for most subscriptions. List prices rose on March 1, 2025, and
  each subscription keeps the edition's per-seat price it was sold at:
  `seat_list_price` before the discount and `seat_price` after it.
* **A sales funnel.** Opportunities are renewals, expansions, and the
  new-business deal behind each customer's first term. Deals at
  companies that never bought are not in the graph, so every
  new-business opportunity is won. Sales cycles run from two to eight
  weeks for SMB and up to eight months for Enterprise. Most renewals
  close in the month before the term ends, and discounts are deeper on
  deals that close at the end of a quarter. Opportunities carry
  `lead_source`, `forecast_category`, `push_count`, `competitor`, and,
  while open, `next_step`. Quotes exist for opportunities created since
  July 2025 and have numbered versions, with earlier ones `superseded`.
* **Payments.** Customers pay by ACH, wire, or card. On the monthly
  plan, about a third of cancellations are involuntary: three failed
  card attempts, then the subscription ends with
  `cancellation_type: 'involuntary'`.
* **People and turnover.** Two account executives and one SDR left in
  2024 and 2025 (`status: 'inactive'`). The account executives own old
  opportunities but no accounts. Account executives have an
  `annual_quota_usd`.
* **Messy records.** Self-serve signups type their own company names,
  sometimes in lowercase, and their own countries ("USA", "UK",
  "Deutschland"), and often leave the industry and size empty. Since the
  hourly CRM sync started on November 3, 2025 (`PipelineChange` 2.3.0),
  18 customers have a second `Customer` record with the same domain, no
  subscriptions, and `status: 'active'`. The seller's own demo tenant,
  "INTERNAL - Sales demo tenant" (`domain: 'company.com'`), is a
  customer with a $0 Enterprise subscription.
* **Usage in ticket text.** Some tickets quote product usage, such as
  how many seats are active, and name product features. The numbers come
  from a usage simulation that the graph does not hold, and a feature a
  ticket names is one the customer's edition and add-ons include.

## The graph

Table 3 lists the labels. The counts are from the generator's output
for the current version.

Table 3. Node labels

| Label | Count | One node per | Key |
| --- | --- | --- | --- |
| `Customer` | 2,411 | paying or former customer, plus CRM duplicates and the demo tenant | `customer_id` |
| `Subscription` | 7,878 | billing term: one year on the yearly plan, one month on the monthly plan | `subscription_id` |
| `Payment` | 10,016 | payment attempt at the payment provider | `transaction_id` |
| `Opportunity` | 5,136 | renewal, expansion, or won new-business deal | `opportunity_id` |
| `Quote` | 2,990 | quote version on an opportunity created since July 2025 | `quote_id` |
| `SupportTicket` | 7,721 | support ticket | `ticket_id` |
| `Employee` | 48 | person at the seller, `@company.com` | `employee_id` |
| `ContractEvent` | 7,482 | event the pipeline derived from a subscription | `event_id` |
| `PipelineChange` | 8 | entry in the data pipeline's change log | `version` |
| `Dashboard` | 2 | dashboard | `dashboard_id` |

The relationships:

```
(:Customer)-[:HAS_SUBSCRIPTION]->(:Subscription)
(:Subscription)-[:RENEWED_AS]->(:Subscription)        next term, starting the day the last one ended
(:Subscription)-[:REACTIVATED_AS]->(:Subscription)    next term, after a lapse
(:Payment)-[:PAYS]->(:Subscription)                   the term a payment is for
(:Payment)-[:PAID_BY]->(:Customer)                    the customer that paid, sometimes a parent company
(:Customer)-[:OWNED_BY]->(:Employee)                  account executive
(:Customer)-[:MANAGED_BY]->(:Employee)                customer success manager
(:Customer)-[:HAS_OPPORTUNITY]->(:Opportunity)
(:Opportunity)-[:OWNED_BY]->(:Employee)
(:Opportunity)-[:RENEWS]->(:Subscription)             the term a renewal deal is for
(:Opportunity)-[:RESULTED_IN]->(:Subscription)        the term a won deal started
(:Opportunity)-[:HAS_QUOTE]->(:Quote)
(:Quote)-[:REVIEWED_BY]->(:Employee)                  revenue operations
(:Quote)-[:APPROVED_BY]->(:Employee)                  finance
(:Customer)-[:HAS_TICKET]->(:SupportTicket)
(:SupportTicket)-[:ASSIGNED_TO]->(:Employee)
(:ContractEvent)-[:FOR_SUBSCRIPTION]->(:Subscription)
(:PipelineChange)-[:AUTHORED_BY]->(:Employee)
(:Dashboard)-[:OWNED_BY]->(:Employee)
(:Employee)-[:REPORTS_TO]->(:Employee)
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
which terms renew inside the two 2026 windows, and lets monthly-plan
customers who churn inside those windows come back only after the
window ends. It
tries seeds in order from `FIRST_SEED` until one meets every target.

A second pass builds everything else from that history: prices and
payments for each term, the opportunities and quotes behind each sale
and renewal, tickets, and the messy records. Changing that pass does not
move the numbers in Table 1.

Near the end of the second pass, `acquisitions()` picks the six bought
customers listed above. For each one it looks for a buyer in the same
country whose next term grew by about the bought customer's seats, or
did not grow at all, and it rewrites the bought customer's note and
loss reason to name that buyer. It changes only text, so no other
record moves. `renewal_paperwork()` then gives the planted ticket to a
Mid-Market customer whose yearly term ends by September 30, 2026. It
changes only the ticket's links to its customer and requester.
`payers()` picks the holding companies and the customers they pay for,
leaving out every customer that the planted records above name. It
starts with Wadebridge Group's two brands: US customers of one account
executive whose yearly terms lapsed last autumn and were renewed weeks
late, and whose current terms end after the renewal review. It
rewrites the notes on the two lapsed terms and then links every
payment to its term and its payer. It draws from its own random stream
and changes only payments and those two notes, so no other record
moves. The run fails if it picks brands other than the two chapter 4
names.

The second pass also simulates contacts, invoices, products, product
usage, sales activities, and prospects with their lost deals, because
ticket text, prices, and the shared random stream depend on them. Its
last step, `simplify()`, moves what the ten labels need into properties:
the main contact onto `Customer`, the requester onto `SupportTicket`,
and the edition and add-ons onto `Subscription` and `Quote`. It then
leaves the simulated labels out of the graph, together with the
opportunities and quotes at prospects. Every record that remains is the
same as in the earlier version of the seed with seventeen labels.
