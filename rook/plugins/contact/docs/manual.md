# Contacts

`contact.search` finds people in the address book of a phone running the
Rook app: every contact whose name or number contains `query`, one row per
number (`{contacts: [{name, number}]}`). A blank query lists the first ones
by name.

The app needs the contacts permission: on the phone, Rook > Settings >
Permissions > "Grant SMS / contacts / calendar / location".
