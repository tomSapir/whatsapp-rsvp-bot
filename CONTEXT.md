# WhatsApp RSVP Bot

The shared language for a bot that invites people to an event over WhatsApp and
collects their attendance confirmations. This glossary defines the domain terms —
not implementation.

## Language

**Event**:
The single occasion this bot collects RSVPs for — in v1 a couple's celebration. The Host
defines it once to start the bot: the couple's names (English and Hebrew), the date, and an
optional image. There is exactly **one** Event per deployment, and every Invitation belongs to
it implicitly.
_Avoid_: wedding, party, occasion, function

**Invitation**:
One entry in the list = one invited party reachable at a single phone number. It is
the unit that gets messaged and that responds. An Invitation covers one or more Guests.
A couple or family invited together is **one** Invitation, not several.
_Avoid_: Guest (when you mean the row/party), contact, recipient, lead

**Guest**:
An individual person who attends, counted within an Invitation's party size. **Every
Guest belongs to exactly one Invitation** — a person is never counted by two Invitations.
A Guest is not messaged directly unless they are also some Invitation's contact.
_Avoid_: Attendee (acceptable alias), invitee, person

**Party size**:
The number of Guests attending under one Invitation, including the contact themselves
("comes alone" = 1). An Invitation that is **not attending has no party size**. Total heads =
the sum of party size across attending Invitations.
_Avoid_: headcount (use for the event-wide total), seats, plus-ones

**Host**:
The person running the bot — sends invitations and reads results. There is one Host.
_Avoid_: admin, organizer, user, owner

**RSVP**:
An Invitation's response: whether it is attending, and if so the party size, dietary needs,
and an optional note. An Invitation has **at most one** RSVP, which always reflects its most
recent Reply — a later Reply can change attendance or party size.
_Avoid_: reply (use for any inbound message), confirmation, response

**Reply**:
Any inbound WhatsApp message — a button tap or free text. A Reply is matched to an Invitation
by phone number; a Reply from a number not on the list belongs to **no** Invitation and is
therefore never an RSVP. An RSVP is assembled from one or more Replies.
_Avoid_: response (means the RSVP), message (the audit-log record of any in/out message)

## Flagged ambiguities

- **Party-size double-counting** *(resolved — Model A)*: a Guest belongs to exactly one
  Invitation. Couples/families invited together are a single Invitation whose party size
  covers the whole group. The system never models persons or links across Invitations, so
  `SUM(party_size)` over attending Invitations is the exact headcount with no double-count.
  Guard: the Host console should flag a person who appears to be invited twice so they can be
  merged before sending.

- **Confirmed vs complete RSVP** *(resolved)*: tapping Yes makes an Invitation *attending*
  (confirmed), but its party size may still be **unknown** — distinct from 1 ("comes alone").
  Such an RSVP is *confirmed but incomplete*. Party size stays unknown until the Invitation
  reports it; it is never silently coerced to 0 or 1. The event headcount is therefore
  reported as **known heads plus a count of attending Invitations of unknown size**, so the
  total can never silently undercount. Dietary and note are optional and never affect whether
  an RSVP is complete.

## Example dialogue

> **Host:** "How many invitations have confirmed?"
> **Dev:** "Forty-two Invitations are attending — that's the row count. But the headcount
> is the sum of their party sizes, which is one hundred and three Guests."
> **Host:** "And Dana's invitation?"
> **Dev:** "One Invitation, party size four — Dana plus three more Guests under her."
