/* Lunchbreak ELO — shared interactions */
(() => {
  "use strict";

  /* Theme toggle — persisted server-side via cookie, so every fresh page
     (including the very first byte of the next navigation) renders with
     data-theme already correct. No client-side re-apply, no reset. */
  document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      const root = document.documentElement;
      const next = root.dataset.theme === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      document.cookie = `theme=${next}; path=/; max-age=31536000; samesite=lax`;
      window.dispatchEvent(new CustomEvent("elo:theme"));
    });
  });

  /* Mobile header menu */
  const header = document.querySelector(".app-header");
  const menuToggle = document.querySelector("[data-menu-toggle]");
  if (header && menuToggle) {
    menuToggle.addEventListener("click", () => {
      const open = header.classList.toggle("is-mobile-open");
      menuToggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
    window.matchMedia("(min-width: 701px)").addEventListener("change", (event) => {
      if (event.matches) {
        header.classList.remove("is-mobile-open");
        menuToggle.setAttribute("aria-expanded", "false");
      }
    });
  }

  /* Close open <details> menus on outside click */
  document.addEventListener("click", (event) => {
    document.querySelectorAll("details[data-auto-close][open]").forEach((menu) => {
      if (!menu.contains(event.target)) menu.removeAttribute("open");
    });
  });

  /* Flash toasts: auto-dismiss + click to dismiss */
  document.querySelectorAll(".flash").forEach((flash, index) => {
    const hide = () => {
      flash.classList.add("is-hiding");
      flash.addEventListener("animationend", () => flash.remove(), { once: true });
    };
    flash.addEventListener("click", hide);
    setTimeout(hide, 5200 + index * 400);
  });

  /* Segmented panel switcher (ladder tables etc.) */
  document.querySelectorAll("[data-tab-group]").forEach((group) => {
    const name = group.dataset.tabGroup;
    const buttons = group.querySelectorAll("button[data-tab-target]");
    const panels = document.querySelectorAll(`[data-panel][data-panel-group="${name}"]`);
    buttons.forEach((button) => {
      button.addEventListener("click", () => {
        buttons.forEach((other) => other.classList.toggle("is-active", other === button));
        panels.forEach((panel) => {
          panel.hidden = panel.dataset.panel !== button.dataset.tabTarget;
        });
        window.dispatchEvent(new Event("resize"));
      });
    });
  });

  /* Copy-to-clipboard buttons */
  document.querySelectorAll("[data-copy]").forEach((button) => {
    button.addEventListener("click", async () => {
      const text = button.dataset.copy;
      try {
        await navigator.clipboard.writeText(text);
      } catch (e) {
        const scratch = document.createElement("textarea");
        scratch.value = text;
        document.body.appendChild(scratch);
        scratch.select();
        document.execCommand("copy");
        scratch.remove();
      }
      button.classList.add("is-copied");
      setTimeout(() => button.classList.remove("is-copied"), 1600);
    });
  });

  /* Match form: game-type dependent fields, custom time control, guest players */
  document.querySelectorAll("form[data-match-form]").forEach((form) => {
    const gameTypeInputs = form.querySelectorAll("[name='game_type']");
    const teamOnly = form.querySelectorAll("[data-team-only]");
    const help = form.querySelector("[data-game-type-copy]");

    const currentGameType = () => {
      const field = form.querySelector("[name='game_type']:checked") || form.querySelector("select[name='game_type']");
      return field ? field.value : "standard";
    };

    const syncGameType = () => {
      const isTeam = currentGameType() === "one_arm_one_brain";
      teamOnly.forEach((node) => {
        node.hidden = !isTeam;
        node.querySelectorAll("select").forEach((field) => { field.disabled = !isTeam; });
      });
      if (help) help.textContent = isTeam ? help.dataset.teamCopy : help.dataset.standardCopy;
    };
    gameTypeInputs.forEach((input) => input.addEventListener("change", syncGameType));
    syncGameType();

    const timeControlSelect = form.querySelector("[data-time-control-select]");
    const customTimeControl = form.querySelector("[data-time-control-custom]");
    const syncTimeControl = () => {
      if (!timeControlSelect || !customTimeControl) return;
      const isCustom = timeControlSelect.value === "custom";
      customTimeControl.hidden = !isCustom;
      customTimeControl.querySelectorAll("[data-time-control-custom-field]").forEach((field) => {
        field.disabled = !isCustom;
      });
    };
    if (timeControlSelect) {
      timeControlSelect.addEventListener("change", syncTimeControl);
      syncTimeControl();
    }

    form.querySelectorAll("[data-guest-side]").forEach((side) => {
      const toggle = side.querySelector("[data-guest-toggle]");
      const selectField = form.querySelector(`[data-player-select="${side.dataset.guestSide}"]`);
      const guestWrap = side.querySelector("[data-guest-input]");
      const guestEmail = side.querySelector("[data-guest-email]");
      if (!toggle || !selectField || !guestWrap || !guestEmail) return;
      const syncGuest = () => {
        const useGuest = toggle.checked;
        selectField.disabled = useGuest;
        selectField.required = !useGuest;
        guestWrap.hidden = !useGuest;
        guestEmail.disabled = !useGuest;
        guestEmail.required = useGuest;
      };
      toggle.addEventListener("change", syncGuest);
      syncGuest();
    });
  });

  /* Sortable tables */
  document.querySelectorAll("[data-sortable-table]").forEach((table) => {
    const tbody = table.querySelector("tbody");
    const buttons = table.querySelectorAll("[data-sort-column]");
    if (!tbody || !buttons.length) return;
    let activeColumn = table.dataset.defaultSort || buttons[0].dataset.sortColumn;
    let activeDirection = "desc";

    const camel = (column) => "sort" + column.split("-").map((part) => part[0].toUpperCase() + part.slice(1)).join("");
    const valueFor = (row, column, type) => {
      const value = row.dataset[camel(column)] || "";
      if (type === "number") return Number.parseFloat(value) || 0;
      if (type === "date") return value ? Date.parse(value) : 0;
      return value.toLocaleLowerCase();
    };
    const update = () => {
      buttons.forEach((button) => {
        const indicator = button.querySelector(".sort-indicator");
        const isActive = button.dataset.sortColumn === activeColumn;
        if (indicator) indicator.textContent = isActive ? (activeDirection === "asc" ? "▲" : "▼") : "↕";
        button.setAttribute("aria-sort", isActive ? (activeDirection === "asc" ? "ascending" : "descending") : "none");
      });
    };
    buttons.forEach((button) => {
      button.addEventListener("click", () => {
        const column = button.dataset.sortColumn;
        const type = button.dataset.sortType;
        activeDirection = activeColumn === column && activeDirection === "asc" ? "desc" : "asc";
        activeColumn = column;
        const direction = activeDirection === "asc" ? 1 : -1;
        Array.from(tbody.querySelectorAll("tr"))
          .sort((left, right) => {
            const a = valueFor(left, column, type);
            const b = valueFor(right, column, type);
            if (a < b) return -direction;
            if (a > b) return direction;
            return 0;
          })
          .forEach((row) => tbody.appendChild(row));
        update();
      });
    });
    update();
  });

  /* Auto-submit filter selects */
  document.querySelectorAll("form[data-auto-submit] select").forEach((select) => {
    select.addEventListener("change", () => select.form.submit());
  });
})();
