// mail_core.js — JXA helper library for cobos-apple-mail-mcp's write layer.
//
// Loaded once per osascript invocation alongside a single function call
// (see write/jxa_executor.py::run_jxa). Every operation here is a real
// Apple Event / JXA method call against Application("Mail") — never
// simulated keystrokes or NSPasteboard injection (CLAUDE.md invariant #5).
//
// CORRECTNESS NOTE: this file was authored against the documented JXA Mail
// scripting dictionary and standard JXA Mail-automation patterns, but has
// not been exercised against a live Mail.app in this build environment.
// Before relying on the write tools, run the manual verification steps in
// the project Wiki (Development & contributing) on a real Mac: dry-run
// previews first, then a single non-destructive move + undo_last.

function MailApp() {
  return Application("Mail");
}

function ping(args) {
  var app = MailApp();
  return { ok: true, running: app.running() };
}

function accountEmailAddresses(account) {
  try {
    return account.emailAddresses() || [];
  } catch (e) {
    return [];
  }
}

function findAccount(app, hint) {
  var accounts = app.accounts();
  if (!hint) {
    return accounts.length ? accounts[0] : null;
  }
  var lowerHint = String(hint).toLowerCase();
  for (var i = 0; i < accounts.length; i++) {
    if (accounts[i].name().toLowerCase() === lowerHint) return accounts[i];
  }
  for (var i = 0; i < accounts.length; i++) {
    var addrs = accountEmailAddresses(accounts[i]);
    for (var j = 0; j < addrs.length; j++) {
      if (String(addrs[j]).toLowerCase() === lowerHint) return accounts[i];
    }
  }
  return null;
}

var MAILBOX_ALIASES = {
  sent: ["sent messages", "sent items", "sent"],
  trash: ["deleted messages", "trash", "bin"],
  drafts: ["drafts"],
  junk: ["junk", "junk e-mail", "spam"],
  inbox: ["inbox"]
};

function findMailbox(account, hint) {
  if (!account) return null;
  var mailboxes = account.mailboxes();
  if (!hint) {
    for (var i = 0; i < mailboxes.length; i++) {
      if (mailboxes[i].name().toLowerCase() === "inbox") return mailboxes[i];
    }
    return mailboxes.length ? mailboxes[0] : null;
  }
  var lowerHint = String(hint).toLowerCase();
  for (var i = 0; i < mailboxes.length; i++) {
    if (mailboxes[i].name().toLowerCase() === lowerHint) return mailboxes[i];
  }
  var aliases = MAILBOX_ALIASES[lowerHint];
  if (aliases) {
    for (var i = 0; i < mailboxes.length; i++) {
      var name = mailboxes[i].name().toLowerCase();
      if (aliases.indexOf(name) !== -1) return mailboxes[i];
    }
  }
  return null;
}

function listAccounts(args) {
  var app = MailApp();
  var accounts = app.accounts();
  var result = [];
  for (var i = 0; i < accounts.length; i++) {
    result.push({
      name: accounts[i].name(),
      emailAddresses: accountEmailAddresses(accounts[i])
    });
  }
  return result;
}

function listMailboxes(args) {
  var app = MailApp();
  var account = findAccount(app, args.account);
  if (!account) return [];
  var mailboxes = account.mailboxes();
  var result = [];
  for (var i = 0; i < mailboxes.length; i++) {
    result.push({ name: mailboxes[i].name(), unreadCount: mailboxes[i].unreadCount() });
  }
  return result;
}

function describeMessage(msg, accountName, mailboxName) {
  var dateSent = null;
  try {
    dateSent = msg.dateSent() ? msg.dateSent().toISOString() : null;
  } catch (e) {}
  return {
    account: accountName,
    mailbox: mailboxName,
    mailInternalId: msg.id(),
    messageId: msg.messageId(),
    subject: msg.subject(),
    dateSent: dateSent
  };
}

// The core resolution primitive: scoped search for a message by its RFC822
// Message-ID (args.messageId, bracketed). Returns every candidate found in
// scope so core/resolver.py can read-back-verify and detect ambiguity —
// this function never picks a "best" match itself.
function resolveMessage(args) {
  var app = MailApp();
  var targetId = args.messageId;
  var candidates = [];

  var accountsToSearch = [];
  if (args.accountHint) {
    var acc = findAccount(app, args.accountHint);
    if (acc) accountsToSearch.push(acc);
  }
  if (accountsToSearch.length === 0) accountsToSearch = app.accounts();

  for (var a = 0; a < accountsToSearch.length; a++) {
    var account = accountsToSearch[a];
    var mailboxesToSearch = [];
    if (args.mailboxHint) {
      var mb = findMailbox(account, args.mailboxHint);
      if (mb) mailboxesToSearch.push(mb);
    }
    if (mailboxesToSearch.length === 0) mailboxesToSearch = account.mailboxes();

    for (var m = 0; m < mailboxesToSearch.length; m++) {
      var mailbox = mailboxesToSearch[m];
      var hits = [];
      try {
        hits = mailbox.messages.whose({ messageId: targetId })();
      } catch (e) {
        hits = [];
      }
      for (var h = 0; h < hits.length; h++) {
        candidates.push(describeMessage(hits[h], account.name(), mailbox.name()));
      }
    }
  }
  return { candidates: candidates };
}

// Re-resolve to exactly one live message OBJECT (not just its description),
// given an already-known account/mailbox name (from a prior resolveMessage
// call) — avoids repeating a broad scan for the actual mutation.
function getMessageHandle(args) {
  var app = MailApp();
  var account = findAccount(app, args.accountHint);
  if (!account) throw "account not found: " + args.accountHint;
  var mailbox = findMailbox(account, args.mailboxHint);
  if (!mailbox) throw "mailbox not found: " + args.mailboxHint;
  var hits = mailbox.messages.whose({ messageId: args.messageId })();
  if (hits.length === 0) throw "message not found on re-resolution (it may have moved)";
  return { app: app, account: account, mailbox: mailbox, message: hits[0] };
}

function moveEmail(args) {
  var handle = getMessageHandle(args);
  var targetMailbox = findMailbox(handle.account, args.toMailbox);
  if (!targetMailbox) throw "target mailbox not found: " + args.toMailbox;
  handle.app.move(handle.message, { to: targetMailbox });
  return { moved: true, toMailbox: targetMailbox.name() };
}

function updateEmailStatus(args) {
  var handle = getMessageHandle(args);
  var msg = handle.message;
  if (args.action === "mark_read") msg.readStatus = true;
  else if (args.action === "mark_unread") msg.readStatus = false;
  else if (args.action === "flag") msg.flaggedStatus = true;
  else if (args.action === "unflag") msg.flaggedStatus = false;
  else throw "unknown status action: " + args.action;
  return { updated: true, action: args.action };
}

function manageTrash(args) {
  var handle = getMessageHandle(args);
  if (args.action === "move_to_trash") {
    var trashMailbox = findMailbox(handle.account, "trash");
    if (!trashMailbox) throw "trash mailbox not found for account " + handle.account.name();
    handle.app.move(handle.message, { to: trashMailbox });
    return { trashed: true };
  }
  if (args.action === "delete_permanent") {
    handle.app.delete(handle.message);
    return { deleted: true };
  }
  throw "unknown trash action: " + args.action;
}

function trashCount(args) {
  var app = MailApp();
  var account = findAccount(app, args.accountHint);
  if (!account) throw "account not found: " + args.accountHint;
  var trashMailbox = findMailbox(account, "trash");
  if (!trashMailbox) throw "trash mailbox not found";
  return { count: trashMailbox.messages().length };
}

function emptyTrash(args) {
  var app = MailApp();
  var account = findAccount(app, args.accountHint);
  if (!account) throw "account not found: " + args.accountHint;
  var trashMailbox = findMailbox(account, "trash");
  if (!trashMailbox) throw "trash mailbox not found";
  var messages = trashMailbox.messages();
  var count = messages.length;
  for (var i = count - 1; i >= 0; i--) {
    app.delete(messages[i]);
  }
  return { emptied: true, count: count };
}

function createMailbox(args) {
  var app = MailApp();
  var account = findAccount(app, args.account);
  if (!account) throw "account not found: " + args.account;
  var parts = String(args.name).split("/");
  var parent = null;
  var created = null;
  for (var i = 0; i < parts.length; i++) {
    var siblings = parent ? parent.mailboxes() : account.mailboxes();
    var existing = null;
    for (var j = 0; j < siblings.length; j++) {
      if (siblings[j].name() === parts[i]) {
        existing = siblings[j];
        break;
      }
    }
    created = existing || app.Mailbox({ name: parts[i] }).make({ at: parent || account });
    parent = created;
  }
  return { created: true, name: created.name() };
}

function addRecipients(outgoing, list, kind) {
  if (!list) return;
  var app = MailApp();
  for (var i = 0; i < list.length; i++) {
    var addr = list[i];
    if (kind === "to") outgoing.toRecipients.push(app.ToRecipient({ address: addr }).make());
    else if (kind === "cc") outgoing.ccRecipients.push(app.CcRecipient({ address: addr }).make());
    else if (kind === "bcc") outgoing.bccRecipients.push(app.BccRecipient({ address: addr }).make());
  }
}

function addAttachments(outgoing, paths) {
  if (!paths || paths.length === 0) return;
  var app = MailApp();
  for (var i = 0; i < paths.length; i++) {
    try {
      outgoing.content.attachments.push(app.Attachment({ fileName: paths[i] }).make());
    } catch (e) {
      // Fail loud: silently dropping an attachment the caller asked for
      // would mean we appear to succeed while sending something different
      // from what was requested.
      throw "failed to attach file " + paths[i] + ": " + e;
    }
  }
}

function finalizeOutgoing(outgoing, mode) {
  outgoing.visible = true;
  if (mode === "send") {
    outgoing.send();
    return "sent";
  }
  // "draft" and "open" both leave a compose window open — Mail's scripting
  // dictionary has no "save silently to Drafts without a window" action.
  return "draft";
}

function composeEmail(args) {
  var app = MailApp();
  var outgoing = app.OutgoingMessage().make({
    withProperties: { subject: args.subject || "", content: args.body || "" }
  });
  if (args.fromAddress) {
    try {
      outgoing.sender = args.fromAddress;
    } catch (e) {}
  }
  addRecipients(outgoing, args.to, "to");
  addRecipients(outgoing, args.cc, "cc");
  addRecipients(outgoing, args.bcc, "bcc");
  addAttachments(outgoing, args.attachments);
  return { status: finalizeOutgoing(outgoing, args.mode || "send") };
}

function replyToEmail(args) {
  var handle = getMessageHandle(args);
  var outgoing = handle.message.reply({ openingWindow: false, replyToAll: !!args.replyAll });
  if (args.body) {
    outgoing.content = args.body + "\n\n" + outgoing.content();
  }
  addRecipients(outgoing, args.cc, "cc");
  addRecipients(outgoing, args.bcc, "bcc");
  addAttachments(outgoing, args.attachments);
  return { status: finalizeOutgoing(outgoing, args.mode || "send") };
}

function forwardEmail(args) {
  var handle = getMessageHandle(args);
  var outgoing = handle.message.forward({ openingWindow: false });
  if (args.message) {
    outgoing.content = args.message + "\n\n" + outgoing.content();
  }
  addRecipients(outgoing, args.to, "to");
  addRecipients(outgoing, args.cc, "cc");
  addRecipients(outgoing, args.bcc, "bcc");
  addAttachments(outgoing, args.attachments);
  return { status: finalizeOutgoing(outgoing, args.mode || "send") };
}

function manageDrafts(args) {
  var app = MailApp();
  var account = findAccount(app, args.account);
  if (!account) throw "account not found: " + args.account;
  var draftsMailbox = findMailbox(account, "drafts");
  if (!draftsMailbox) throw "drafts mailbox not found";

  if (args.action === "list") {
    var msgs = draftsMailbox.messages();
    var result = [];
    for (var i = 0; i < msgs.length; i++) {
      result.push(describeMessage(msgs[i], account.name(), draftsMailbox.name()));
    }
    return { drafts: result };
  }

  if (args.action === "create") {
    var outgoing = app.OutgoingMessage().make({
      withProperties: { subject: args.subject || "", content: args.body || "" }
    });
    addRecipients(outgoing, args.to, "to");
    addRecipients(outgoing, args.cc, "cc");
    addRecipients(outgoing, args.bcc, "bcc");
    addAttachments(outgoing, args.attachments);
    outgoing.visible = true;
    return { created: true };
  }

  // Unsent drafts have no reliable RFC822 Message-ID yet, so send/open/
  // delete locate the draft by subject within the Drafts mailbox only — a
  // much narrower, lower-risk scope than a whole-mailbox subject search.
  var draftMsgs = draftsMailbox.messages();
  var matches = [];
  for (var d = 0; d < draftMsgs.length; d++) {
    if (String(draftMsgs[d].subject()).indexOf(args.draftSubject) !== -1) {
      matches.push(draftMsgs[d]);
    }
  }
  if (matches.length === 0) throw "no draft found matching subject: " + args.draftSubject;
  if (matches.length > 1) {
    throw "ambiguous draft subject (" + matches.length + " matches): " + args.draftSubject;
  }
  var draft = matches[0];

  if (args.action === "delete") {
    app.delete(draft);
    return { deleted: true };
  }
  if (args.action === "open") {
    draft.openingWindow = true;
    return { opened: true };
  }
  if (args.action === "send") {
    throw "Mail's scripting dictionary has no direct send-existing-draft " +
      "action; open the draft and send it manually, or recreate it via compose_email";
  }
  throw "unknown drafts action: " + args.action;
}
