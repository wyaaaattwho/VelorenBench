use super::*;
use crate::hud::Hud;
use common::{assets::AssetExt, trade::{TradeAction, TradePhase}};
use conrod_core::{Ui, UiBuilder};

struct Fixture {
    ui: Ui,
    modal: widget::Id,
    under: widget::Id,
    font: conrod_core::text::font::Id,
    image: conrod_core::image::Id,
}

impl Fixture {
    fn new() -> Self {
        let mut ui = UiBuilder::new([1920.0, 1080.0]).build();
        let raw = crate::ui::ice::RawFont::load("voxygen.font.haxrcorp_4089_cyrillic_altgr")
            .unwrap().cloned();
        let font = ui.fonts.insert(conrod_core::text::Font::from_bytes(raw.0).unwrap());
        let image = conrod_core::image::Map::new().insert(());
        let modal = ui.widget_id_generator().next();
        let under = ui.widget_id_generator().next();
        Self { ui, modal, under, font, image }
    }

    fn draw(&mut self, prompt: Option<&PromptDialogSettings>) -> Option<DialogOutcomeEvent> {
        let mut cell = self.ui.set_widgets();
        let window = cell.window;
        let outcome = prompt.and_then(|prompt_dialog_settings| PromptDialog {
            images: [self.image; 6], font: self.font, font_sizes: [16, 18],
            accept_key: "Y".into(), decline_key: "N".into(),
            common: widget::CommonBuilder::default(), prompt_dialog_settings,
        }.set(self.modal, &mut cell));
        // Stock HUD also draws underlying controls after the prompt.
        assert!(!Button::image(self.image).w_h(1920.0, 1080.0)
            .middle_of(window).parent(window).set(self.under, &mut cell).was_clicked(),
            "modal pointer must not click through to an underlying HUD button");
        use conrod_core::event::Widget as Input;
        assert!(!cell.widget_input(self.under).events().any(|event| matches!(event,
            Input::Press(_) | Input::Release(_) | Input::Click(_) | Input::DoubleClick(_)
                | Input::Drag(_) | Input::Scroll(_))),
            "underlying HUD must not receive modal button, drag or scroll input");
        outcome
    }

    fn warm(&mut self, prompt: &PromptDialogSettings) {
        for _ in 0..3 { assert!(self.draw(Some(prompt)).is_none()); }
    }

    fn control(&self, accept: bool, label: bool) -> [f64; 2] {
        let state = self.ui.widget_graph().widget(self.modal).unwrap()
            .unique_widget_state::<PromptDialog>().unwrap();
        let id = match (accept, label) {
            (true, false) => state.state.ids.accept_key,
            (true, true) => state.state.ids.accept_txt,
            (false, false) => state.state.ids.decline_key,
            (false, true) => state.state.ids.decline_txt,
        };
        self.ui.rect_of(id).unwrap().xy()
    }

    fn pointer(&mut self, xy: [f64; 2], gesture: &str) {
        let destination = (gesture == "drag").then_some([1100.0, 750.0]);
        for event in Hud::native_pointer_events(xy[0] + 960.0, 540.0 - xy[1],
                                                [1920,1080], gesture, destination, None) {
            self.ui.handle_event(event);
        }
    }
}

fn prompt(negative: Option<Event>) -> PromptDialogSettings {
    PromptDialogSettings::new(
        "Really give away these items in exchange for nothing?".into(),
        Event::TradeAction(TradeAction::Accept(TradePhase::Review)), negative)
}

#[test]
fn native_modal_visible_accept_and_decline_use_stock_events_and_labels() {
    for label in [false, true] {
        for accept in [false, true] {
            let prompt = prompt(Some(Event::TradeAction(TradeAction::Decline)));
            let mut fixture = Fixture::new();
            fixture.warm(&prompt);
            fixture.pointer(fixture.control(accept, label), "left_click");
            let outcome = fixture.draw(Some(&prompt)).expect("visible stock modal control");
            let expected_id = prompt.id;
            let mut current = Some(prompt);
            let event = PromptDialogSettings::finish(&mut current, outcome);
            assert!(current.is_none(), "actual stock producer must close the modal");
            let Event::PromptDialogOutcome(id, Some(event)) = event else { panic!("stock producer origin"); };
            assert_eq!(id, expected_id);
            let Event::TradeAction(actual) = *event else { panic!("must keep actual stock event"); };
            assert_eq!(actual, if accept { TradeAction::Accept(TradePhase::Review) } else { TradeAction::Decline });
            assert!(fixture.draw(None).is_none(), "consumed click cannot replay after close");
        }
    }
}

#[test]
fn native_modal_has_pointer_priority_outside_controls_for_all_gestures() {
    let prompt = prompt(None);
    let mut fixture = Fixture::new();
    fixture.warm(&prompt);
    for gesture in ["left_click", "right_click", "middle_click", "hover", "scroll_up", "scroll_down", "drag"] {
        fixture.pointer([0.0, -120.0], gesture);
        assert!(fixture.draw(Some(&prompt)).is_none(), "background gesture cannot confirm or close");
    }
}

#[test]
fn native_modal_changed_prompt_does_not_consume_old_click() {
    let old = prompt(None);
    let replacement = prompt(Some(Event::DeclineInvite));
    assert_ne!(old.id, replacement.id, "identical-looking prompts need different identities");
    let mut fixture = Fixture::new();
    fixture.warm(&old);
    fixture.pointer(fixture.control(true, false), "left_click");
    assert!(fixture.draw(Some(&replacement)).is_none(), "never accept a new prompt with an old click");
}

#[test]
fn native_modal_optional_negative_and_ok_preserve_stock_behavior() {
    let prompt = prompt(None);
    let mut fixture = Fixture::new();
    fixture.warm(&prompt);
    fixture.pointer(fixture.control(false, true), "left_click");
    let outcome = fixture.draw(Some(&prompt)).expect("decline with no negative action still closes");
    assert!(matches!(prompt.outcome_event(outcome), Event::PromptDialogOutcome(_, None)));

    let ok = PromptDialogSettings::new("Acknowledgement".into(),
        Event::AcknowledgePersistenceLoadError, None).with_no_negative_option();
    let mut fixture = Fixture::new();
    fixture.warm(&ok);
    fixture.pointer(fixture.control(true, true), "left_click");
    assert!(matches!(fixture.draw(Some(&ok)), Some(DialogOutcomeEvent::Affirmative(
        Event::AcknowledgePersistenceLoadError))));
}

#[test]
fn native_modal_keyboard_bindings_still_produce_the_original_stock_outcomes() {
    for accept in [false, true] {
        let mut prompt = prompt(None);
        let mut fixture = Fixture::new();
        fixture.warm(&prompt);
        prompt.set_outcome_via_keypress(accept);
        let outcome = fixture.draw(Some(&prompt)).expect("original keyboard path");
        assert!(matches!((accept, outcome), (true, DialogOutcomeEvent::Affirmative(_))
            | (false, DialogOutcomeEvent::Negative(None))));
    }
}
