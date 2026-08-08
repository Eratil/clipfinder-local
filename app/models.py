from pydantic import BaseModel, Field


class CollectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class RatingUpdate(BaseModel):
    rating: str = Field(pattern="^(unrated|accepted|rejected)$")
    review_reason: str = Field(default="", max_length=80)


class RejectionReasonCreate(BaseModel):
    reason: str = Field(min_length=1, max_length=80)


class SegmentTimingUpdate(BaseModel):
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)


class ComposerCaptionRefresh(BaseModel):
    """A one-off caption transcription for the Composer's unsaved range."""

    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)


class SegmentTranscriptUpdate(BaseModel):
    transcript: str = Field(max_length=5000)


class SegmentCensorUpdate(BaseModel):
    censor_profanity: bool


class SegmentPauseTrimUpdate(BaseModel):
    remove_pauses: bool


class TagFeedbackUpdate(BaseModel):
    tag: str = Field(min_length=1, max_length=80)
    verdict: str = Field(pattern="^(correct|incorrect|unmarked)$")


class PublicationFeedbackUpdate(BaseModel):
    platform: str = Field(default="", max_length=32)
    published_url: str = Field(default="", max_length=2000)
    views: int = Field(default=0, ge=0)
    average_watch_percent: float = Field(default=0, ge=0, le=1000)
    shares: int = Field(default=0, ge=0)
    comments: int = Field(default=0, ge=0)


class ChatDelayUpdate(BaseModel):
    delay_seconds: float = Field(default=6, ge=0, le=60)


class ExampleCreate(BaseModel):
    segment_id: str


class SimilaritySearch(BaseModel):
    video_id: str
    limit: int = Field(default=25, ge=1, le=100)


class DescriptionSearch(BaseModel):
    video_id: str
    description: str = Field(min_length=3, max_length=1000)
    limit: int = Field(default=25, ge=1, le=100)


class ReferenceFolderImport(BaseModel):
    folder_path: str = Field(min_length=1, max_length=1000)
    include_subfolders: bool = True


class ReferenceUrlImport(BaseModel):
    source_url: str = Field(min_length=10, max_length=2000)


class RemotePreviewCreate(BaseModel):
    """One-off analysis of a public short without retaining the source media."""

    source_url: str = Field(min_length=10, max_length=2000)


class RemotePreviewSave(BaseModel):
    pattern_set_id: str = Field(min_length=1, max_length=80)


class DiscoveryPatternSetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    profile: str = Field(default="general", pattern="^(general|soulslike|conversation|horror|game_quote_reaction|funny_moments|game_reactions|chat_interactions|opinions)$")


class SavedPromptCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=3, max_length=1000)


class RemoteVideoCreate(BaseModel):
    source_url: str = Field(min_length=10, max_length=2000)
    analysis_mode: str = Field(default="default", pattern="^(fast|default|extended)$")


class ExportRequest(BaseModel):
    lead_in_seconds: float = Field(default=0, ge=0, le=10)
    lead_out_seconds: float = Field(default=0, ge=0, le=10)
    start_seconds: float | None = Field(default=None, ge=0)
    end_seconds: float | None = Field(default=None, ge=0)
    hook_seconds: float = Field(default=0, ge=0)
    captions_preset: str = Field(default="none", pattern="^(none|clean|highlight|minimal|boxed_pop|neon_gaming|cinematic|karaoke_punch|minimal_center)$")
    caption_position: str = Field(default="bottom", pattern="^(top|two_fifths|middle|four_fifths|bottom)$")
    base_color: str = Field(default="#FFFFFF", pattern="^#[0-9A-Fa-f]{6}$")
    active_color: str = Field(default="#FFFF00", pattern="^#[0-9A-Fa-f]{6}$")
    font_family: str = Field(default="Inter", pattern="^(Inter|Montserrat|Poppins|Lato|Roboto Condensed|Oswald|Nunito|Noto Sans|Bungee|Cinzel|Pixelify Sans)$")
    outline_enabled: bool = True
    outline_color: str = Field(default="#000000", pattern="^#[0-9A-Fa-f]{6}$")
    glow_enabled: bool = False
    opacity: int = Field(default=100, ge=20, le=100)
    max_lines: int = Field(default=2, ge=1, le=4)
    layout: str = Field(default="original", pattern="^(original|portrait_camera|portrait_game|portrait_split)$")
    audio_track: int = Field(default=1, ge=1, le=4)
    censor_profanity: bool | None = None
    remove_pauses: bool | None = None
    microphone_enhancement: bool = False
    normalize_loudness: bool = False
    volume_gain_db: float = Field(default=0, ge=-12, le=12)
    camera_x: float = Field(default=0.78, ge=0, le=1)
    camera_y: float = Field(default=0.03, ge=0, le=1)
    camera_width: float = Field(default=0.11, gt=0.02, le=1)
    camera_height: float = Field(default=0.11, gt=0.02, le=1)
    game_x: float = Field(default=0.22, ge=0, le=1)
    game_y: float = Field(default=0.0, ge=0, le=1)
    game_width: float = Field(default=0.56, gt=0.02, le=1)
    game_height: float = Field(default=1.0, gt=0.02, le=1)
    filename: str = Field(default="", max_length=120)
    # Composer can refresh captions for an unsaved timing range. Keep those
    # words in the export request instead of overwriting the analysed clip.
    caption_text: str | None = Field(default=None, max_length=50000)
    caption_word_timestamps: list[dict] | None = None


class CaptionDefaultsUpdate(BaseModel):
    captions_preset: str = Field(default="highlight", pattern="^(none|clean|highlight|minimal|boxed_pop|neon_gaming|cinematic|karaoke_punch|minimal_center)$")
    base_color: str = Field(default="#FFFFFF", pattern="^#[0-9A-Fa-f]{6}$")
    active_color: str = Field(default="#FFFF00", pattern="^#[0-9A-Fa-f]{6}$")
    font_family: str = Field(default="Inter", pattern="^(Inter|Montserrat|Poppins|Lato|Roboto Condensed|Oswald|Nunito|Noto Sans|Bungee|Cinzel|Pixelify Sans)$")
    outline_enabled: bool = True
    outline_color: str = Field(default="#000000", pattern="^#[0-9A-Fa-f]{6}$")
    glow_enabled: bool = False
    opacity: int = Field(default=100, ge=20, le=100)
    max_lines: int = Field(default=2, ge=1, le=4)


class ExportDefaultsUpdate(BaseModel):
    layout: str = Field(default="original", pattern="^(original|portrait_camera|portrait_game|portrait_split)$")
    audio_track: int = Field(default=1, ge=1, le=4)
    camera_x: float = Field(default=0.78, ge=0, le=1)
    camera_y: float = Field(default=0.03, ge=0, le=1)
    camera_width: float = Field(default=0.11, gt=0.02, le=1)
    camera_height: float = Field(default=0.11, gt=0.02, le=1)
    game_x: float = Field(default=0.22, ge=0, le=1)
    game_y: float = Field(default=0.0, ge=0, le=1)
    game_width: float = Field(default=0.56, gt=0.02, le=1)
    game_height: float = Field(default=1.0, gt=0.02, le=1)


class LayoutPresetCreate(ExportDefaultsUpdate):
    name: str = Field(min_length=1, max_length=80)


class AnalysisAudioDefaultsUpdate(BaseModel):
    mode: str = Field(default="split", pattern="^(single|split)$")
    single_track: int = Field(default=1, ge=1, le=4)
    microphone_track: int = Field(default=2, ge=1, le=4)
    all_sounds_track: int = Field(default=1, ge=1, le=4)
    game_track: int = Field(default=3, ge=1, le=4)
    use_all_sounds: bool = True
    use_game: bool = True


class DiscoveryDefaultsUpdate(BaseModel):
    active_profile: str = Field(default="general", pattern="^(general|soulslike|conversation|horror|game_quote_reaction|funny_moments|game_reactions|chat_interactions|opinions)$")
    pattern_set_id: str = Field(default="", max_length=80)
    profanity_filter: str = Field(default="allow", pattern="^(allow|one|none)$")


class CaptionFavoriteCreate(CaptionDefaultsUpdate):
    name: str = Field(min_length=1, max_length=80)
