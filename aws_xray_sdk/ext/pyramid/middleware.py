import pyramid.renders
from pyramid.request import Request

from aws_xray_sdk.core.models import http
from aws_xray_sdk.core.utils import stacktrace
from aws_xray_sdk.core.exceptions.exceptions import SegmentNotFoundException
from aws_xray_sdk.ext.util import calculate_sampling_decision, \
    calculate_segment_name, construct_xray_header, prepare_response_header
from aws_xray_sdk.core.lambda_launcher import check_in_lambda, LambdaContext
from aws_xray_sdk.core import xray_recorder


_recorder = xray_recorder
in_lambda_ctx = False


class TestRequestFactory(Request):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        global _recorder

        self.add_finished_callback(_before_request)
        self.add_response_callback(_after_request)
        self.add_finished_callback(_handle_exception)


class XRayMiddleware(object):
    def __init__(self, app, recorder):
        self.app = app
        self.app.logger.info("initializing xray middleware")
        global _recorder
        _recorder = recorder
        _patch_render(recorder)


def __create_segment(request, recorder, lambda_ctx=False):
    headers = request.headers
    xray_header = construct_xray_header(headers)

    name = calculate_segment_name(request.host, recorder)

    sampling_req = {
        'host': request.host,
        'method': request.method,
        'path': request.path,
        'service': name,
    }
    sampling_decision = calculate_sampling_decision(
        trace_header=xray_header,
        recorder=recorder,
        sampling_req=sampling_req,
    )

    if in_lambda_ctx:
        segment = recorder.begin_subsegment(name)
    else:
        segment = recorder.begin_segment(
            name=name,
            traceid=xray_header.root,
            parent_id=xray_header.parent,
            sampling=sampling_decision,
        )
    segment.save_origin_trace_header(xray_header)

    return segment


def _before_request(request):
    global in_lambda_ctx
    global _recorder

    if check_in_lambda() and type(_recorder.context) == LambdaContext:
        in_lambda_ctx = True

    headers = request.headers
    segment = __create_segment(request, _recorder, lambda_ctx=in_lambda_ctx)

    segment.put_http_meta(http.URL, request.path_url)
    segment.put_http_meta(http.METHOD, request.method)
    segment.put_http_meta(http.USER_AGENT, headers.get('User-Agent'))

    client_ip = headers.get('X-Forwarded-For') or headers.get('HTTP_X_FORWARDED_FOR')
    if client_ip:
        segment.put_http_meta(http.CLIENT_IP, client_ip)
        segment.put_http_meta(http.X_FORWARDED_FOR, True)
    else:
        segment.put_http_meta(http.CLIENT_IP, request.remote_addr)


def _after_request(request, response):
    global in_lambda_ctx
    global _recorder
    in_lambda_ctx = False

    if check_in_lambda() and type(_recorder.context) == LambdaContext:
        in_lambda_ctx = True

    if in_lambda_ctx:
        segment = _recorder.current_subsegment()
    else:
        try:
            segment = _recorder.current_segment()
        except SegmentNotFoundException:
            segment = __create_segment(request, _recorder, lambda_ctx=in_lambda_ctx)
    segment.put_http_meta(http.STATUS, response.status_code)

    origin_header = segment.get_origin_trace_header()
    resp_header_str = prepare_response_header(origin_header, segment)
    response.headers[http.XRAY_HEADER] = resp_header_str

    cont_len = response.headers.get('Content-Length')
    if cont_len:
        segment.put_http_meta(http.CONTENT_LENGTH, int(cont_len))

    if in_lambda_ctx:
        _recorder.end_subsegment()
    else:
        _recorder.end_segment()
    return response


def _handle_exception(request):
    global in_lambda_ctx
    global _recorder

    if not request.exception:
        return
    segment = None
    try:
        if in_lambda_ctx:
            try:
                segment = _recorder.current_subsegment()
            except SegmentNotFoundException:
                segment = __create_segment(request, _recorder, lambda_ctx=in_lambda_ctx)
        else:
            try:
                segment = _recorder.current_segment()
            except SegmentNotFoundException:
                segment = __create_segment(request, _recorder, lambda_ctx=in_lambda_ctx)
    except Exception:
        pass
    if not segment:
        return

    segment.put_http_meta(http.STATUS, 500)
    stack = stacktrace.get_stacktrace(limit=_recorder._max_trace_back)
    segment.add_exception(request.exception, stack)
    if in_lambda_ctx:
        _recorder.end_subsegment()
    else:
        _recorder.end_segment()


def _patch_render(recorder):

    _render = pyramid.renderers.render

    @recorder.capture('template_render')
    def _traced_render(renderer_name, value, request=None):
        recorder.current_subsegment().name = renderer_name
        return _render(renderer_name, value, request=request)

    pyramid.renderers.render = _traced_render
