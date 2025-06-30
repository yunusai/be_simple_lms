from ninja import Schema
from typing import Optional, List
from datetime import datetime
from pydantic import EmailStr, field_validator
from datetime import date


from django.contrib.auth.models import User

class UserOut(Schema):
    id: int
    email: str
    first_name: str
    last_name: str


class CourseSchemaOut(Schema):
    id: int
    name: str
    description: str
    price: int
    max_students: Optional[int]
    image : Optional[str]
    teacher: UserOut
    created_at: datetime
    updated_at: datetime

class CourseMemberOut(Schema):
    id: int 
    course_id: CourseSchemaOut
    user_id: UserOut
    roles: str
    # created_at: datetime


class CourseSchemaIn(Schema):
    name: str
    description: str
    price: int


class CourseContentMini(Schema):
    id: int
    name: str
    description: str
    course_id: CourseSchemaOut
    scheduled_release: Optional[datetime]
    created_at: datetime
    updated_at: datetime


class CourseContentFull(Schema):
    id: int
    name: str
    description: str
    video_url: Optional[str]
    file_attachment: Optional[str]
    course_id: CourseSchemaOut
    scheduled_release: Optional[datetime]
    created_at: datetime
    updated_at: datetime

class CourseCommentOut(Schema):
    id: int
    content_id: CourseContentMini
    member_id: CourseMemberOut
    comment: str
    approved: bool
    created_at: datetime
    updated_at: datetime

class CourseCommentIn(Schema):
    comment: str

class CommentModerationIn(Schema):
    approved: bool

class UserRegisterIn(Schema):
    email: EmailStr
    password: str
    first_name: str
    last_name: str
    tempat_lahir: str
    tanggal_lahir: date
    alamat: str
    no_hp: str

    @field_validator('password')
    def password_must_be_strong(cls, v):
        if len(v) < 8:
            raise ValueError('Password minimal 8 karakter')
        return v

class EnrollStudentBatch(Schema):
    course_id: int
    emails: List[str]

class UserActivityOut(Schema):
    student_enrollment_count: int
    teacher_course_count: int
    comment_count: int
    completed_content_count: int = 0

class CourseAnalyticsOut(Schema):
    course_id: int
    member_count: int
    content_count: int
    comment_count: int
    
class CertificateData(Schema):
    user_name: str
    course_name: str
    completion_date: str

