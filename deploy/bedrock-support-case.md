# Bedrock model-access support case — Opus 4.8 & Sonnet 5

**Status:** ready to submit. This account (`924056189531`) is on **Basic
support**, which cannot open a *technical* support case via Support Center and
has no Support API — so this must be submitted through one of the routes at the
bottom. All evidence below was re-verified live on 2026-07-20.

---

## Subject

Bedrock: models report AUTHORIZED via get-foundation-model-availability but Converse returns AccessDenied "not available for this account" (us-east-1)

## Case details

- **Account:** 924056189531
- **Region:** us-east-1
- **Service:** Amazon Bedrock (and the Anthropic-operated "Claude Platform on AWS" / Mantle model-catalog surface)
- **Affected models:**
  - `anthropic.claude-opus-4-8` (inference profile `us.anthropic.claude-opus-4-8`)
  - `anthropic.claude-sonnet-5` (inference profile `us.anthropic.claude-sonnet-5`)

## Problem

Both models show fully authorized on the control plane, yet every inference
call is rejected as "not available for this account." The account has accepted
each model's usage agreement (`create-foundation-model-agreement` succeeded for
both) and has a use-case questionnaire on file.

`aws bedrock get-foundation-model-availability` for **both** models returns:

```
agreementAvailability.status : AVAILABLE
authorizationStatus          : AUTHORIZED
entitlementAvailability      : AVAILABLE
regionAvailability           : AVAILABLE
```

But `aws bedrock-runtime converse` against either model returns:

```
AccessDeniedException: anthropic.claude-opus-4-8 is not available for this account.
```

This is not an IAM problem and not a code problem:

- The identical code path and IAM role invoke `us.anthropic.claude-sonnet-4-5-20250929-v1:0`
  successfully on this same account — so credentials, region, and inference-profile
  usage are all correct.
- The failure reproduces on **two independent surfaces**: the classic
  `bedrock-runtime converse` API (AWS-operated) and the Bedrock "Mantle" model-catalog
  playground (Anthropic-operated "Claude Platform on AWS"), which returns a native
  Anthropic `403 permission_error` with the same "not available for this account" text.
  For comparison, a non-Anthropic model (Grok) invokes successfully in that same
  playground — so the block is specific to these two Claude listings.

The gap is between the control plane (says AUTHORIZED) and whatever data-plane
entitlement component Converse checks (still rejects), and it has persisted far
beyond any normal propagation window.

## Evidence — request IDs

| Surface | Model | Result | Request ID | When |
| --- | --- | --- | --- | --- |
| bedrock-runtime Converse | `us.anthropic.claude-opus-4-8` | 403 AccessDenied | `1d58d646-5e4f-450b-9a22-85799e51c165` | 2026-07-20 02:16:24 GMT |
| Mantle playground | `anthropic.claude-opus-4-8` | 403 permission_error | `req_ognuj6ecaolkx46oplk7f2uood7xt3siwqx3zn3pdh6lhugtubiq` | earlier |
| Mantle playground | `anthropic.claude-sonnet-5` | 403 permission_error | `req_l65rm3n6bwpoh4dqequysujf6cfk56dw5735wze5t74n7twzg6qa` | earlier |

## Ask

Please reconcile the data-plane entitlement for these two models on account
924056189531 in us-east-1 so that Converse succeeds, given the control plane
already reports AUTHORIZED / AVAILABLE. If the fix is owned by Anthropic (the
Mantle surface is Anthropic-operated), please route accordingly.

---

## Where to submit (Basic-support routes)

1. **AWS Sales / contact form (no cost, what the error itself points to):**
   https://aws.amazon.com/contact-us/sales-support/ — paste the Subject + Case
   details above. This is the route the AccessDenied message explicitly
   recommends ("For additional access options, contact AWS Sales").
2. **Anthropic support** — since the second failing surface is the
   Anthropic-operated "Claude Platform on AWS," Anthropic may own the fix.
   Reference the same evidence and request IDs.
3. **Formal AWS technical case (optional, costs money):** temporarily upgrade to
   **Developer support** (~$29/mo) in the Support plans console, open a
   Bedrock technical case with the above, then downgrade. Only worth it if you
   want an SLA-backed ticket rather than the sales/contact route.
