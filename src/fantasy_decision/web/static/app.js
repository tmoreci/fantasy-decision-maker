// Tiny bit of client behaviour HTMX doesn't cover: the 2-3 player limit and
// adding searched players to the picks.

const MAX_PICKS = 3;
const MIN_PICKS = 2;

function pickForm() {
  return document.getElementById("pick-form");
}

function playerBoxes() {
  return Array.from(pickForm().querySelectorAll('input[name="player"]'));
}

function refreshPicks() {
  const form = pickForm();
  if (!form) return;
  const boxes = playerBoxes();
  const checked = boxes.filter((box) => box.checked);
  const full = checked.length >= MAX_PICKS;
  boxes.forEach((box) => { box.disabled = full && !box.checked; });

  document.getElementById("decide-button").disabled = checked.length < MIN_PICKS;
  const names = checked.map((box) => box.dataset.label);
  const count = document.getElementById("pick-count");
  if (checked.length === 0) count.textContent = "No players picked yet.";
  else if (checked.length < MIN_PICKS) count.textContent = `Picked ${names[0]}. Pick at least one more.`;
  else count.textContent = `Comparing ${names.join(" vs ")}.` + (full ? " That's the max of three." : "");

  // Searched players that were un-ticked disappear rather than cluttering the list.
  document.querySelectorAll("#picked .chip").forEach((chip) => {
    if (!chip.querySelector("input").checked) chip.remove();
  });
}

function addPick(id, label, detail) {
  const existing = playerBoxes().find((box) => box.value === id);
  if (existing) {
    existing.checked = true;
  } else {
    if (playerBoxes().filter((box) => box.checked).length >= MAX_PICKS) return;
    const chip = document.createElement("label");
    chip.className = "chip";
    const box = document.createElement("input");
    Object.assign(box, { type: "checkbox", name: "player", value: id, checked: true });
    box.dataset.label = label;
    const text = document.createElement("span");
    text.textContent = label;
    const small = document.createElement("small");
    small.textContent = detail;
    text.append(small);
    chip.append(box, text);
    document.getElementById("picked").append(chip);
  }
  const search = pickForm().querySelector('input[name="q"]');
  search.value = "";
  document.getElementById("search-results").innerHTML = "";
  refreshPicks();
}

document.addEventListener("change", (event) => {
  if (event.target.name === "player") refreshPicks();
});

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-add-player]");
  if (button) addPick(button.dataset.addPlayer, button.dataset.label, button.dataset.detail);
});

document.addEventListener("htmx:afterSwap", (event) => {
  if (event.detail.target.id === "roster") refreshPicks();
  if (event.detail.target.id === "result") {
    event.detail.target.scrollIntoView({ behavior: "smooth", block: "start" });
  }
});

document.addEventListener("DOMContentLoaded", refreshPicks);
