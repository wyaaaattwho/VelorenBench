use super::{TEXT_COLOR, UI_HIGHLIGHT_0, img_ids::Imgs};
use crate::{
    game_input::GameInput,
    hud::{Event, PromptDialogSettings},
    settings::Settings,
    ui::fonts::Fonts,
};
use conrod_core::{
    Colorable, Labelable, Positionable, Sizeable, Widget, WidgetCommon,
    widget::{self, Button, Image, Text},
    widget_ids,
};
use i18n::LocalizationHandle;

widget_ids! {
    struct Ids {
        barrier,
        top,
        mid,
        bot,
        accept_txt, // optional timer
        accept_key, //button with label
        decline_txt,
        decline_key,
        prompt_txt,
    }
}
#[derive(WidgetCommon)]
pub struct PromptDialog<'a> {
    images: [conrod_core::image::Id; 6],
    font: conrod_core::text::font::Id,
    font_sizes: [u32; 2],
    accept_key: String,
    decline_key: String,
    #[conrod(common_builder)]
    common: widget::CommonBuilder,
    prompt_dialog_settings: &'a PromptDialogSettings,
}

impl<'a> PromptDialog<'a> {
    pub fn new(
        imgs: &'a Imgs,
        fonts: &'a Fonts,
        _localized_strings: &'a LocalizationHandle,
        settings: &'a Settings,
        prompt_dialog_settings: &'a PromptDialogSettings,
    ) -> Self {
        Self {
            images: [imgs.prompt_top, imgs.prompt_mid, imgs.prompt_bot,
                     imgs.key_button, imgs.close_btn_hover, imgs.close_btn_press],
            font: fonts.cyri.conrod_id,
            font_sizes: [fonts.cyri.scale(16), fonts.cyri.scale(18)],
            accept_key: settings.controls.get_binding(GameInput::AcceptGroupInvite)
                .map_or_else(|| "".into(), |key| key.display_string()),
            decline_key: settings.controls.get_binding(GameInput::DeclineGroupInvite)
                .map_or_else(|| "".into(), |key| key.display_string()),
            common: widget::CommonBuilder::default(),
            prompt_dialog_settings,
        }
    }
}

pub struct State {
    ids: Ids,
    prompt_id: u64,
}

pub enum DialogOutcomeEvent {
    Affirmative(Event),
    Negative(Option<Event>),
}

impl Widget for PromptDialog<'_> {
    type Event = Option<DialogOutcomeEvent>;
    type State = State;
    type Style = ();

    fn init_state(&self, id_gen: widget::id::Generator) -> Self::State {
        State {
            ids: Ids::new(id_gen),
            prompt_id: self.prompt_dialog_settings.id,
        }
    }

    fn style(&self) -> Self::Style {}

    fn update(self, args: widget::UpdateArgs<Self>) -> Self::Event {
        common_base::prof_span!("PromptDialog::update");
        let widget::UpdateArgs { state, ui, .. } = args;
        let mut event: Option<DialogOutcomeEvent> = None;
        let same_prompt = state.prompt_id == self.prompt_dialog_settings.id;
        state.update(|state| state.prompt_id = self.prompt_dialog_settings.id);

        // A transparent, floating stock Conrod parent catches the entire
        // pointer surface. Underlying HUD stays visible but cannot receive
        // clicks, drags or scrolls while this confirmation is in front.
        widget::Rectangle::fill([ui.win_w, ui.win_h])
            .color(conrod_core::color::TRANSPARENT)
            .middle_of(ui.window)
            .parent(ui.window)
            .floating(true)
            .set(state.ids.barrier, ui);

        // Window
        Image::new(self.images[0])
            .w_h(276.0, 24.0)
            .mid_top_with_margin_on(ui.window, 100.0)
            .color(Some(UI_HIGHLIGHT_0))
            .parent(state.ids.barrier)
            .set(state.ids.top, ui);
        if !self.prompt_dialog_settings.message.is_empty() {
            Image::new(self.images[1])
            .w(276.0)
            .h_of(state.ids.prompt_txt) // height relative to content, max height 150
            .down_from(state.ids.top, 0.0)
            .color(Some(UI_HIGHLIGHT_0))
            .scroll_kids_vertically()
            .parent(state.ids.barrier)
            .set(state.ids.mid, ui);
        }
        Image::new(self.images[2])
            .w_h(276.0, 35.0)
            .down_from(state.ids.mid, 0.0)
            .color(Some(UI_HIGHLIGHT_0))
            .parent(state.ids.barrier)
            .set(state.ids.bot, ui);

        // Accept/Decline Buttons
        if (Button::image(self.images[3])
            .w_h(20.0, 20.0)
            .hover_image(self.images[4])
            .press_image(self.images[5])
            .label(&self.accept_key)
            .image_color(UI_HIGHLIGHT_0)
            .label_color(TEXT_COLOR)
            .label_font_size(self.font_sizes[0])
            .label_font_id(self.font)
            .label_y(conrod_core::position::Relative::Scalar(2.5))
            .label_x(conrod_core::position::Relative::Scalar(0.5))
            .bottom_right_with_margins_on(state.ids.bot, 4.0, 6.0)
            .parent(state.ids.barrier)
            .set(state.ids.accept_key, ui)
            .was_clicked() && same_prompt)
            || self
                .prompt_dialog_settings
                .outcome_via_keypress
                .is_some_and(|outcome| outcome)
        {
            // Primary use should be through pressing the key instead of clicking this
            event = Some(DialogOutcomeEvent::Affirmative(
                self.prompt_dialog_settings.affirmative_event.clone(),
            ));
        }
        let accept_txt = if self.prompt_dialog_settings.negative_option {
            "Accept"
        } else {
            "Ok"
        };
        Text::new(accept_txt)
            .bottom_left_with_margins_on(state.ids.accept_key, 4.0, -65.0)
            .font_id(self.font)
            .font_size(self.font_sizes[1])
            .color(TEXT_COLOR)
            .graphics_for(state.ids.accept_key)
            .parent(state.ids.barrier)
            .set(state.ids.accept_txt, ui);

        if self.prompt_dialog_settings.negative_option {
            if (Button::image(self.images[3])
                .w_h(20.0, 20.0)
                .hover_image(self.images[4])
                .press_image(self.images[5])
                .label(&self.decline_key)
                .image_color(UI_HIGHLIGHT_0)
                .label_color(TEXT_COLOR)
                .label_font_size(self.font_sizes[0])
                .label_font_id(self.font)
                .label_y(conrod_core::position::Relative::Scalar(2.5))
                .label_x(conrod_core::position::Relative::Scalar(0.5))
                .bottom_left_with_margins_on(state.ids.bot, 4.0, 6.0)
                .parent(state.ids.barrier)
                .set(state.ids.decline_key, ui)
                .was_clicked() && same_prompt)
                || self
                    .prompt_dialog_settings
                    .outcome_via_keypress
                    .is_some_and(|outcome| !outcome)
            {
                event = Some(DialogOutcomeEvent::Negative(
                    self.prompt_dialog_settings.negative_event.as_ref().cloned(),
                ));
            }
            Text::new("Decline")
                .bottom_left_with_margins_on(state.ids.decline_key, 4.0, 28.0)
                .font_id(self.font)
                .font_size(self.font_sizes[1])
                .color(TEXT_COLOR)
                .graphics_for(state.ids.decline_key)
                .parent(state.ids.barrier)
                .set(state.ids.decline_txt, ui);
        }

        // Prompt Description
        Text::new(&self.prompt_dialog_settings.message)
        .mid_top_with_margin_on(state.ids.mid,0.0)
        .font_id(self.font)
        .font_size(self.font_sizes[1])
        .color(TEXT_COLOR)
        .w(260.0) // Text stays within frame
        .parent(state.ids.barrier)
        .set(state.ids.prompt_txt, ui);

        event
    }
}

#[cfg(test)]
#[path = "prompt_dialog_tests.rs"]
mod native_modal_tests;
