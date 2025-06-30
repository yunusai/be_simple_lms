from django.contrib import admin
from lms_core.models import Course, CourseContent

@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ["name", "price", "description", "teacher", 'created_at']
    list_filter = ["teacher"]
    search_fields = ["name", "description"]
    readonly_fields = ["created_at", "updated_at"]
    fields = ["name", "description", "price", "image", "teacher", "created_at", "updated_at"]
    
@admin.register(CourseContent)
class CourseContentAdmin(admin.ModelAdmin):
    list_display = ('name', 'course_id', 'scheduled_release', 'is_released')
    list_filter = ('course_id',)
    
    def is_released(self, obj):
        from django.utils import timezone
        return not obj.scheduled_release or obj.scheduled_release <= timezone.now()
    is_released.boolean = True
    is_released.short_description = "Dirilis"