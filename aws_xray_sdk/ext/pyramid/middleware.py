from pyramid.request import Request
import pyramid.renderers

from aws_xray_sdk.core.models import http
from aws_xray_sdk.core.utils import stacktrace
from aws_xray_sdk.ext.util import calculate_sampling_decision, \
    calculate_segment_name, construct_xray_header, prepare_response_header
from aws_xray_sdk.core.lambda_launcher import check_in_lambda, LambdaContext


class XRayMiddleware(object):

    def __init__(self, app, request, recorder):
        self.app = app
        self.app.logger.info("initializing xray middleware")

        self._recorder = recorder
        request.add_finished_callback(self._before_request)
        request.add_response_callback(self._after_request)
        # request.add_finished_callback(self._handle_exception)
        self.in_lambda_ctx = False

        if check_in_lambda() and type(self._recorder.context) == LambdaContext:
            self.in_lambda_ctx = True

        _patch_render(recorder)

    def _before_request(self, request):
        headers = request.headers
        xray_header = construct_xray_header(headers)

        name = calculate_segment_name(request.host, self._recorder)

        sampling_req = {
            'host': request.host,
            'method': request.method,
            'path': request.path,
            'service': name,
        }
        sampling_decision = calculate_sampling_decision(
            trace_header=xray_header,
            recorder=self._recorder,
            sampling_req=sampling_req,
        )

        if self.in_lambda_ctx:
            segment = self._recorder.begin_subsegment(name)
        else:
            segment = self._recorder.begin_segment(
                name=name,
                traceid=xray_header.root,
                parent_id=xray_header.parent,
                sampling=sampling_decision,
            )

        segment.save_origin_trace_header(xray_header)
        segment.put_http_meta(http.URL, request.path_url)
        segment.put_http_meta(http.METHOD, request.method)
        segment.put_http_meta(http.USER_AGENT, headers.get('User-Agent'))

        client_ip = headers.get('X-Forwarded-For') or headers.get('HTTP_X_FORWARDED_FOR')
        if client_ip:
            segment.put_http_meta(http.CLIENT_IP, client_ip)
            segment.put_http_meta(http.X_FORWARDED_FOR, True)
        else:
            segment.put_http_meta(http.CLIENT_IP, request.remote_addr)

    def _after_request(self, request, response):
        if self.in_lambda_ctx:
            segment = self._recorder.current_subsegment()
        else:
            segment = self._recorder.current_segment()
        segment.put_http_meta(http.STATUS, response.status_code)

        origin_header = segment.get_origin_trace_header()
        resp_header_str = prepare_response_header(origin_header, segment)
        response.headers[http.XRAY_HEADER] = resp_header_str

        cont_len = response.headers.get('Content-Length')
        if cont_len:
            segment.put_http_meta(http.CONTENT_LENGTH, int(cont_len))

        if self.in_lambda_ctx:
            self._recorder.end_subsegment()
        else:
            self._recorder.end_segment()
        return response

    def _handle_exception(self, exception):
        if not exception:
            return
        segment = None
        try:
            if self.in_lambda_ctx:
                segment = self._recorder.current_subsegment()
            else:
                segment = self._recorder.current_segment()
        except Exception:
            pass
        if not segment:
            return

        segment.put_http_meta(http.STATUS, 500)
        stack = stacktrace.get_stacktrace(limit=self._recorder._max_trace_back)
        segment.add_exception(exception, stack)
        if self.in_lambda_ctx:
            self._recorder.end_subsegment()
        else:
            self._recorder.end_segment()


def _patch_render(recorder):

    _render = pyramid.renderers.render

    @recorder.capture('template_render')
    def _traced_render(renderer_name, value, request=None):
        recorder.current_subsegment().name = renderer_name
        return _render(renderer_name, value, request=request)

    pyramid.renderers.render = _traced_render
