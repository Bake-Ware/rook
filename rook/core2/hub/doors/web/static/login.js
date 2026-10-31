"use strict";
if (/[?&]failed=1/.test(location.search)) document.getElementById("failed").hidden = false;
const next = new URLSearchParams(location.search).get("next");
if (next) document.getElementById("next").value = next;
