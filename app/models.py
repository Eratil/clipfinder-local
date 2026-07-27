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


class SegmentTranscriptUpdate(BaseModel):
    transcript: str = Field(max_length=5000)


class SegmentCensorUpdate(BaseModel):
    censor_profanity: bool


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


class SavedPromptCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=3, max_length=1000)


class RemoteVideoCreate(BaseModel):
    source_url: str = Field(min_length=10, max_length=2000)


class ExportRequest(BaseModel):
    lead_in_seconds: float = Field(default=0, ge=0, le=10)
    lead_out_seconds: float = Field(default=0, ge=0, le=10)
    captions_preset: str = Field(default="none", pattern="^(none|clean|highlight|minimal)$")
    caption_position: str = Field(default="bottom", pattern="^(top|middle|bottom)$")
    base_color: str = Field(default="#FFFFFF", pattern="^#[0-9A-Fa-f]{6}$")
    active_color: str = Field(default="#FFFF00", pattern="^#[0-9A-Fa-f]{6}$")
    layout: str = Field(default="original", pattern="^(original|portrait_camera|portrait_game|portrait_split)$")
    audio_track: int = Field(default=1, ge=1, le=4)
    filename: str = Field(default="", max_length=120)


class CaptionDefaultsUpdate(BaseModel):
    captions_preset: str = Field(default="highlight", pattern="^(none|clean|highlight|minimal)$")
    base_color: str = Field(default="#FFFFFF", pattern="^#[0-9A-Fa-f]{6}$")
    active_color: str = Field(default="#FFFF00", pattern="^#[0-9A-Fa-f]{6}$")


class ExportDefaultsUpdate(BaseModel):
    layout: str = Field(default="original", pattern="^(original|portrait_camera|portrait_game|portrait_split)$")
    audio_track: int = Field(default=1, ge=1, le=4)


class AnalysisAudioDefaultsUpdate(BaseModel):
    mode: str = Field(default="split", pattern="^(single|split)$")
    single_track: int = Field(default=1, ge=1, le=4)
    microphone_track: int = Field(default=2, ge=1, le=4)
    all_sounds_track: int = Field(default=1, ge=1, le=4)
    game_track: int = Field(default=3, ge=1, le=4)
    use_all_sounds: bool = True
    use_game: bool = True


class DiscoveryDefaultsUpdate(BaseModel):
    active_profile: str = Field(default="general", pattern="^(general|soulslike|conversation|horror)$")


class CaptionFavoriteCreate(CaptionDefaultsUpdate):
    name: str = Field(min_length=1, max_length=80)
